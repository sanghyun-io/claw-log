"""
Claw-Log Notion Module
urllib 기반 Notion REST API 클라이언트 및 마크다운→Notion 블록 변환.
"""

import json
import re
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# =============================================================================
#  예외
# =============================================================================

class NotionAPIError(Exception):
    """Notion API 호출 실패."""

    def __init__(self, status, message, hint=""):
        self.status = status
        self.message = message
        self.hint = hint
        super().__init__(f"[{status}] {message}")


# =============================================================================
#  NotionClient
# =============================================================================

class NotionClient:
    BASE_URL = "https://api.notion.com/v1"
    NOTION_VERSION = "2025-09-03"

    def __init__(self, token: str):
        self.token = token

    # ── HTTP 공통 ──

    def _request(self, method, endpoint, data=None) -> dict:
        """Notion API HTTP 요청. 인증 헤더 + JSON 처리."""
        url = f"{self.BASE_URL}{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": self.NOTION_VERSION,
            "Content-Type": "application/json",
        }
        body = json.dumps(data).encode("utf-8") if data else None
        req = Request(url, data=body, headers=headers, method=method)

        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
            status = e.code
            if status == 401:
                raise NotionAPIError(
                    401, "Notion 토큰이 유효하지 않습니다.",
                    "claw-log --notion-setup 으로 토큰을 재설정하세요.",
                )
            if status == 403:
                raise NotionAPIError(
                    403, "페이지 접근 권한이 없습니다.",
                    "Notion 앱에서 해당 페이지에 Integration을 연결해주세요.",
                )
            if status == 404:
                raise NotionAPIError(
                    404, "페이지 또는 Database를 찾을 수 없습니다.",
                    "ID를 확인하세요.",
                )
            if status == 429:
                raise NotionAPIError(429, "Notion API Rate Limit 초과.")
            if status >= 500:
                raise NotionAPIError(status, f"Notion 서버 오류: {body_text}")
            raise NotionAPIError(status, f"Notion API 오류: {body_text}")
        except URLError as e:
            reason = str(e.reason) if hasattr(e, 'reason') else str(e)
            raise NotionAPIError(0, f"네트워크 오류: {reason}", "인터넷 연결을 확인하세요.")
        except (TimeoutError, OSError) as e:
            raise NotionAPIError(0, f"Notion API 연결 실패: {e}")

    # ── 토큰 검증 ──

    def validate_token(self) -> bool:
        """토큰 유효성 검증 (GET /users/me)"""
        try:
            self._request("GET", "/users/me")
            return True
        except NotionAPIError:
            return False

    # ── 페이지 검색 ──

    def search_pages(self) -> list[dict]:
        """Integration에 연결된 모든 페이지를 검색 (POST /search)."""
        result = self._request("POST", "/search", {
            "filter": {"value": "page", "property": "object"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
        })
        pages = []
        for page in result.get("results", []):
            title = _extract_title(page)
            pages.append({
                "id": page["id"],
                "title": title or "(제목 없음)",
                "url": page.get("url", ""),
            })
        return pages

    # ── Database 관리 ──

    def create_database(self, parent_page_id: str) -> tuple[str, str]:
        """부모 페이지 아래에 'Career Logs' Database 생성. 반환: (database_id, data_source_id)."""
        result = self._request("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": "Career Logs"}}],
            "initial_data_source": {
                "properties": {
                    "Name": {"type": "title", "title": {}},
                    "Date": {"type": "date", "date": {}},
                },
            },
        })
        database_id = result["id"]
        data_source_id = result["data_sources"][0]["id"]
        return database_id, data_source_id

    def find_database(self, parent_page_id: str) -> tuple[str, str] | tuple[None, None]:
        """부모 페이지의 자식 블록에서 기존 Career Logs DB 검색. 반환: (database_id, data_source_id)."""
        result = self._request("GET", f"/blocks/{parent_page_id}/children?page_size=100")
        for block in result.get("results", []):
            if block.get("type") == "child_database":
                raw_title = block.get("child_database", {}).get("title", "")
                # title은 문자열 또는 rich_text 배열일 수 있음
                if isinstance(raw_title, str):
                    db_title = raw_title
                elif isinstance(raw_title, list):
                    db_title = "".join(
                        t.get("plain_text", "") if isinstance(t, dict) else str(t)
                        for t in raw_title
                    )
                else:
                    db_title = str(raw_title)
                if "Career Logs" in db_title:
                    database_id = block["id"]
                    db_result = self._request("GET", f"/databases/{database_id}")
                    data_sources = db_result.get("data_sources", [])
                    if data_sources:
                        return database_id, data_sources[0]["id"]
                    return database_id, None
        return None, None

    def ensure_database(
        self,
        parent_page_id: str,
        database_id: str | None = None,
        data_source_id: str | None = None,
    ) -> tuple[str, str]:
        """(database_id, data_source_id) 쌍을 확보. 있으면 검증, 없으면 find→create."""
        if database_id:
            try:
                db_result = self._request("GET", f"/databases/{database_id}")
                data_sources = db_result.get("data_sources", [])
                ds_id = data_source_id or (data_sources[0]["id"] if data_sources else None)
                if ds_id:
                    return database_id, ds_id
            except NotionAPIError:
                pass
        found_db_id, found_ds_id = self.find_database(parent_page_id)
        if found_db_id and found_ds_id:
            return found_db_id, found_ds_id
        return self.create_database(parent_page_id)

    # ── 페이지 CRUD ──

    def find_page_by_date(self, data_source_id: str, date_str: str) -> str | None:
        """해당 날짜의 페이지가 이미 있는지 조회 (중복 방지)."""
        result = self._request("POST", f"/data_sources/{data_source_id}/query", {
            "filter": {
                "property": "Date",
                "date": {"equals": date_str},
            },
        })
        results = result.get("results", [])
        if results:
            return results[0]["id"]
        return None

    def find_page_by_name(self, data_source_id: str, title: str) -> str | None:
        """Name(title) 프로퍼티로 페이지 조회 — 마이그레이션 시 엔트리 단위 중복 방지."""
        result = self._request("POST", f"/data_sources/{data_source_id}/query", {
            "filter": {
                "property": "Name",
                "title": {"equals": title},
            },
        })
        results = result.get("results", [])
        if results:
            return results[0]["id"]
        return None

    def create_page(self, data_source_id: str, title: str, date_str: str, blocks: list) -> str:
        """Database에 새 페이지 생성. 반환: page URL."""
        # Notion API는 children을 최대 100개까지만 허용
        first_chunk = blocks[:100]
        remaining = blocks[100:]
        result = self._request("POST", "/pages", {
            "parent": {"data_source_id": data_source_id},
            "properties": {
                "Name": {"title": [{"text": {"content": title}}]},
                "Date": {"date": {"start": date_str}},
            },
            "children": first_chunk,
        })
        page_id = result["id"]
        # 나머지 blocks를 100개씩 append
        self._append_blocks(page_id, remaining)
        return result.get("url", "")

    def update_page_content(self, page_id: str, blocks: list):
        """기존 페이지의 내용을 교체 (같은 날짜 재실행 시)."""
        # 1. 기존 children 전부 삭제 (pagination 처리)
        self._delete_all_children(page_id)
        # 2. 새 blocks 추가 (100개 단위)
        self._append_blocks(page_id, blocks)

    def _delete_all_children(self, block_id: str):
        """블록의 모든 자식을 pagination으로 순회하며 삭제."""
        start_cursor = None
        while True:
            endpoint = f"/blocks/{block_id}/children?page_size=100"
            if start_cursor:
                endpoint += f"&start_cursor={start_cursor}"
            result = self._request("GET", endpoint)
            for block in result.get("results", []):
                try:
                    self._request("DELETE", f"/blocks/{block['id']}")
                except NotionAPIError:
                    pass
            if not result.get("has_more"):
                break
            start_cursor = result.get("next_cursor")

    def _append_blocks(self, parent_id: str, blocks: list):
        """blocks를 100개 단위로 분할하여 append."""
        for i in range(0, len(blocks), 100):
            chunk = blocks[i:i + 100]
            self._request("PATCH", f"/blocks/{parent_id}/children", {"children": chunk})


# =============================================================================
#  유틸리티
# =============================================================================

def _extract_title(page: dict) -> str:
    """Notion page 객체에서 제목 텍스트 추출."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in parts)
    return ""


# =============================================================================
#  마크다운 → Notion 블록 변환
# =============================================================================

RICH_TEXT_LIMIT = 2000


def _parse_inline(text: str) -> list[dict]:
    """마크다운 인라인 서식을 Notion rich_text 배열로 변환."""
    segments = []
    # bold(**) 및 code(`) 패턴 매칭
    pattern = re.compile(r"(\*\*(.+?)\*\*|`(.+?)`)")
    pos = 0
    for m in pattern.finditer(text):
        # 매치 전 일반 텍스트
        if m.start() > pos:
            plain = text[pos:m.start()]
            if plain:
                segments.append(_rich_text(plain))
        if m.group(2) is not None:
            # bold
            segments.append(_rich_text(m.group(2), bold=True))
        elif m.group(3) is not None:
            # code
            segments.append(_rich_text(m.group(3), code=True))
        pos = m.end()
    # 나머지 일반 텍스트
    if pos < len(text):
        remaining = text[pos:]
        if remaining:
            segments.append(_rich_text(remaining))
    if not segments:
        segments.append(_rich_text(text))
    return segments


def _rich_text(content: str, bold=False, code=False) -> dict:
    """단일 rich_text 요소 생성."""
    annotations = {}
    if bold:
        annotations["bold"] = True
    if code:
        annotations["code"] = True
    rt = {"type": "text", "text": {"content": content[:RICH_TEXT_LIMIT]}}
    if annotations:
        rt["annotations"] = annotations
    return rt


def _make_block(block_type: str, rich_text: list[dict]) -> dict:
    """Notion block 객체 생성."""
    return {"object": "block", "type": block_type, block_type: {"rich_text": rich_text}}


def _split_long_text(rich_text_list: list[dict]) -> list[list[dict]]:
    """rich_text가 2000자를 넘으면 분할."""
    total = sum(len(rt["text"]["content"]) for rt in rich_text_list)
    if total <= RICH_TEXT_LIMIT:
        return [rich_text_list]
    # 단순 분할: 각 요소를 2000자 단위로 쪼갬
    chunks = []
    current = []
    current_len = 0
    for rt in rich_text_list:
        content = rt["text"]["content"]
        while content:
            available = RICH_TEXT_LIMIT - current_len
            piece = content[:available]
            content = content[available:]
            new_rt = dict(rt)
            new_rt["text"] = {"content": piece}
            current.append(new_rt)
            current_len += len(piece)
            if current_len >= RICH_TEXT_LIMIT:
                chunks.append(current)
                current = []
                current_len = 0
    if current:
        chunks.append(current)
    return chunks


def md_to_notion_blocks(markdown: str) -> list[dict]:
    """마크다운 문자열을 Notion block 리스트로 변환."""
    blocks = []
    lines = markdown.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 빈 줄 무시
        if not stripped:
            i += 1
            continue

        # 구분선
        if re.match(r"^-{3,}$", stripped):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        # heading_2
        m = re.match(r"^##\s+(.+)$", stripped)
        if m:
            rt = _parse_inline(m.group(1))
            blocks.append(_make_block("heading_2", rt))
            i += 1
            continue

        # heading_3
        m = re.match(r"^###\s+(.+)$", stripped)
        if m:
            rt = _parse_inline(m.group(1))
            blocks.append(_make_block("heading_3", rt))
            i += 1
            continue

        # quote
        if stripped.startswith("> "):
            rt = _parse_inline(stripped[2:])
            blocks.append(_make_block("quote", rt))
            i += 1
            continue

        # bulleted list item
        m = re.match(r"^[-*]\s+(.+)$", stripped)
        if m:
            rt = _parse_inline(m.group(1))
            for chunk in _split_long_text(rt):
                blocks.append(_make_block("bulleted_list_item", chunk))
            i += 1
            continue

        # 일반 텍스트 → paragraph
        rt = _parse_inline(stripped)
        for chunk in _split_long_text(rt):
            blocks.append(_make_block("paragraph", chunk))
        i += 1

    return blocks
