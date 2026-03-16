import re
import datetime
from pathlib import Path
import os

LOG_FILENAME = "career_logs.md"
LOG_FILE = Path.home() / ".claw-log" / LOG_FILENAME


def save_log(summary: str, date_label: str = None, notion_client=None,
             notion_ds_id: str = None, notion_page_id: str = None) -> dict:
    """통합 저장 함수. Notion 우선 + 로컬 백업.

    Returns:
        {"notion": bool, "local": bool,
         "notion_url": str|None, "local_path": str|None,
         "error": str|None}
    """
    result = {"notion": False, "local": False, "notion_url": None, "local_path": None, "error": None}

    # Notion date는 항상 ISO 형식 (오늘 날짜)
    notion_date = datetime.date.today().strftime("%Y-%m-%d")
    display_label = date_label if date_label else notion_date

    # Notion 저장 시도
    if notion_client and notion_ds_id:
        try:
            from claw_log.notion import md_to_notion_blocks
            blocks = md_to_notion_blocks(summary)
            title = f"Career Log - {display_label}"

            # 중복 체크 — title 기반으로 --days 범위별 고유 페이지 식별
            existing_page_id = notion_client.find_page_by_name(notion_ds_id, title)
            if existing_page_id:
                notion_client.update_page_content(existing_page_id, blocks)
                # 기존 페이지 URL 조회
                result["notion_url"] = f"https://notion.so/{existing_page_id.replace('-', '')}"
            else:
                url = notion_client.create_page(notion_ds_id, title, notion_date, blocks)
                result["notion_url"] = url
            result["notion"] = True
        except Exception as e:
            result["error"] = f"Notion 저장 실패: {e}"

    # 빈 summary 방어
    if not summary.strip():
        result["error"] = "AI 요약 결과가 비어있습니다."
        return result

    # 로컬 저장 (항상 실행)
    saved_path = prepend_to_log_file(summary, date_label=date_label)
    if saved_path:
        result["local"] = True
        result["local_path"] = str(saved_path)

    return result


def parse_all_log_entries(filename=LOG_FILENAME) -> tuple:
    """전체 로그 파일을 파싱하여 date별 엔트리 목록을 반환.

    Returns:
        (entries, error) — entries는 {"date": str, "label": str, "content": str} 리스트.
        날짜 범위(YYYY-MM-DD ~ YYYY-MM-DD)는 마지막 날짜를 Notion date로 사용.
    """
    file_path = LOG_FILE

    if not file_path.exists():
        return [], "로그 파일이 없습니다. 먼저 'claw-log'를 실행하세요."

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return [], f"로그 파일 읽기 실패: {e}"

    if not content.strip():
        return [], "로그 파일이 비어있습니다."

    parts = re.split(r"(?=^## 📅 )", content, flags=re.MULTILINE)
    entries = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        lines = part.split("\n", 1)
        header_match = re.match(r"^## 📅 (.+)$", lines[0].strip())
        if not header_match:
            continue

        label = header_match.group(1).strip()

        range_match = re.match(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", label)
        if range_match:
            date = range_match.group(2)
        else:
            single_match = re.match(r"(\d{4}-\d{2}-\d{2})", label)
            date = single_match.group(1) if single_match else None

        if not date:
            continue

        body = lines[1].strip() if len(lines) > 1 else ""
        body = re.sub(r"\n---\s*$", "", body).rstrip()

        entries.append({"date": date, "label": label, "content": body})

    return entries, None


def read_recent_logs(n=5, filename=LOG_FILENAME):
    """최근 N개의 로그 엔트리를 반환합니다. 각 엔트리는 '## 📅' 헤더로 구분."""
    file_path = LOG_FILE

    if not file_path.exists():
        return None, "로그 파일이 없습니다. 먼저 'claw-log'를 실행하세요."

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return None, f"로그 파일 읽기 실패: {e}"

    if not content.strip():
        return None, "로그 파일이 비어있습니다."

    # ## 📅 날짜 헤더로 엔트리 분할
    parts = re.split(r"(?=^## 📅 )", content, flags=re.MULTILINE)
    entries = [p.rstrip().rstrip("-").rstrip() for p in parts if p.strip()]

    if not entries:
        return None, "로그 엔트리를 찾을 수 없습니다."

    return entries[:n], None


_ERROR_LOG_PATTERNS = (
    "[Quota Error]", "[API Key Error]", "[OAuth Error]",
    "[API Error]", "[Network Error]", "[Unknown Error]", "[Model Error]",
)


def remove_error_log_entries(filename=LOG_FILENAME):
    """career_logs.md에서 에러 메시지만 담긴 엔트리를 제거합니다.

    에러 패턴(_ERROR_LOG_PATTERNS)이 포함된 엔트리를 찾아 제거하며,
    나머지 정상 엔트리는 유지합니다.

    Returns:
        int — 제거된 엔트리 수
    """
    file_path = LOG_FILE
    if not file_path.exists():
        return 0

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return 0

    parts = re.split(r"(?=^## 📅 )", content, flags=re.MULTILINE)
    kept = []
    removed = 0
    for part in parts:
        if not part.strip():
            continue
        if any(p in part for p in _ERROR_LOG_PATTERNS):
            removed += 1
        else:
            kept.append(part)

    if removed > 0:
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("".join(kept))
        except Exception:
            return 0

    return removed


def prepend_to_log_file(summary, filename=LOG_FILENAME, date_label=None):
    """
    로그 파일 최상단에 새로운 로그를 추가합니다. (최신순)
    date_label: 커스텀 날짜 레이블 (예: "2026-02-06 ~ 2026-02-12"). None이면 오늘 날짜.
    """
    file_path = LOG_FILE
    file_path.parent.mkdir(parents=True, exist_ok=True)
    existing_content = ""

    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                existing_content = f.read()
        except Exception as e:
            print(f"⚠️ 기존 로그 파일 읽기 실패: {e}")

    label = date_label if date_label else datetime.date.today().strftime("%Y-%m-%d")
    header = f"## 📅 {label}\n\n"
    separator = "\n---\n\n"
    
    # 최신 내용이 뒤에 오는 것이 아니라 앞에 오도록 (Prepend)
    final_content = header + summary + separator + existing_content
    
    # 파일 쓰기
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(final_content)
        return file_path
    except Exception as e:
        print(f"❌ 로그 파일 저장 실패: {e}")
        return None
