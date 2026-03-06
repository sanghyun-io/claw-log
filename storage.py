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

            # 중복 체크
            existing_page_id = notion_client.find_page_by_date(notion_ds_id, notion_date)
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

    # 로컬 저장 (항상 실행)
    saved_path = prepend_to_log_file(summary, date_label=date_label)
    if saved_path:
        result["local"] = True
        result["local_path"] = str(saved_path)

    return result


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
