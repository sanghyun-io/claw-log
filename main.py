import os
import sys
import argparse
import subprocess
import datetime
from pathlib import Path
from dotenv import load_dotenv

try:
    from importlib.metadata import version as _pkg_version, PackageNotFoundError
    __version__ = _pkg_version("claw-log-sh")
except (ImportError, PackageNotFoundError):
    __version__ = "unknown"

from claw_log.engine import GeminiSummarizer, OpenAISummarizer, CodexOAuthSummarizer
from claw_log.storage import prepend_to_log_file, read_recent_logs, save_log, LOG_FILENAME, LOG_FILE
from claw_log.scheduler import install_schedule, show_schedule, remove_schedule, get_schedule_summary
from claw_log.state import load_state, save_state, get_last_hash, acquire_run_lock, release_run_lock
from claw_log.git_collector import (
    get_repo_key, get_latest_commit_hash, collect_project_commits,
    CommitData, ProjectCommits, PER_COMMIT_DIFF_LIMIT,
)

# .env 파일은 현재 작업 디렉토리(CWD)에서 찾습니다.
ENV_PATH = Path(os.getcwd()) / ".env"


def safe_input(prompt, default=""):
    """input() wrapper that handles EOFError for non-interactive environments."""
    try:
        return input(prompt)
    except EOFError:
        print(default)
        return default


# ── 프로젝트 탐색 & 선택 (공용 로직) ──

def discover_git_repos(base_path_str, max_depth=3):
    """
    주어진 경로에서 Git 저장소를 재귀 탐색합니다.
    반환: [(repo_path, is_direct)] 리스트
    - is_direct=True: 입력 경로 자체가 git repo
    - is_direct=False: 하위에서 재귀 발견
    """
    base = Path(base_path_str).resolve()
    
    if not base.exists():
        print(f"⚠️  경로를 찾을 수 없습니다: {base}")
        return []
    
    # 자기 자신이 git repo인 경우 → 직접 지정
    if (base / ".git").exists():
        return [(base, True)]
    
    # 하위 탐색 → 자동 발견
    repos = []
    def _scan(current, depth):
        if depth > max_depth:
            return
        try:
            for entry in sorted(current.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if (entry / ".git").exists():
                    repos.append((entry, False))
                else:
                    _scan(entry, depth + 1)
        except PermissionError:
            pass
    
    _scan(base, 1)
    return repos


def discover_and_select(raw_paths_str, existing_selected=None):
    """
    프로젝트 탐색 → 키보드 선택 UI → 선택된 경로 리스트 반환.
    
    Args:
        raw_paths_str: 쉼표 구분 경로 문자열
        existing_selected: 기존에 선택된 경로 set (프로젝트 관리 시 유지용)
    
    Returns:
        (selected_paths: list[str], input_paths: str)
    """
    import questionary
    
    raw_paths = [p.strip() for p in raw_paths_str.split(",") if p.strip()]
    
    if not raw_paths:
        print("⚠️ 경로가 입력되지 않았습니다.")
        return [], ""
    
    # 1. 전체 탐색
    all_repos = []  # [(path, is_direct)]
    for p in raw_paths:
        found = discover_git_repos(p)
        all_repos.extend(found)
    
    if not all_repos:
        print("⚠️ Git 저장소를 찾지 못했습니다.")
        return [], raw_paths_str
    
    # 중복 제거 (경로 기준)
    seen = set()
    unique_repos = []
    for repo_path, is_direct in all_repos:
        key = str(repo_path)
        if key not in seen:
            seen.add(key)
            unique_repos.append((repo_path, is_direct))
    
    # 2. 선택지 구성
    choices = []
    for repo_path, is_direct in unique_repos:
        path_str = str(repo_path)
        tag = "직접 지정" if is_direct else "자동 발견"
        label = f"{repo_path.name:<30s}  ({tag})  {repo_path}"
        
        # 초기 선택 상태 결정
        if existing_selected is not None:
            checked = path_str in existing_selected
        else:
            checked = is_direct  # 직접 지정=선택, 자동 발견=해제
        
        choices.append(questionary.Choice(
            title=label,
            value=path_str,
            checked=checked,
        ))
    
    # 3. 인터랙티브 선택 UI
    print(f"\n🔍 Git 저장소 {len(unique_repos)}개 발견 — Space로 토글, Enter로 확정")
    
    selected = questionary.checkbox(
        "분석할 프로젝트를 선택하세요:",
        choices=choices,
        instruction="(↑↓ 이동, Space 선택/해제, a 전체선택, Enter 확정)",
    ).ask()
    
    if selected is None:
        # Ctrl+C 등으로 취소
        print("⚠️ 선택이 취소되었습니다.")
        return [], raw_paths_str
    
    if not selected:
        print("⚠️ 최소 1개 이상 선택해야 합니다.")
        return [], raw_paths_str
    
    print(f"\n✅ {len(selected)}개 프로젝트가 선택되었습니다.")
    for p in selected:
        print(f"   📂 {Path(p).name} → {p}")
    
    return selected, raw_paths_str


def show_projects():
    """현재 등록된 프로젝트 목록을 출력합니다."""
    load_dotenv(ENV_PATH, override=True)
    paths_env = os.getenv("PROJECT_PATHS", "")
    
    if not paths_env:
        print("\n⚠️ 등록된 프로젝트가 없습니다.")
        print("   👉 'claw-log --projects' 로 프로젝트를 추가하세요.")
        return
    
    paths = [p.strip() for p in paths_env.split(",") if p.strip()]
    
    print(f"\n📋 현재 등록된 프로젝트 ({len(paths)}개)")
    print("=" * 50)
    for i, p in enumerate(paths, 1):
        name = Path(p).name
        exists = "✅" if Path(p).exists() else "❌ (경로 없음)"
        is_git = "" if not Path(p).exists() else ("" if (Path(p) / ".git").exists() else " ⚠️ .git 없음")
        print(f"   {i}. {name:<30s} {exists}{is_git}")
        print(f"      {p}")
    print("=" * 50)


def manage_projects():
    """프로젝트 관리 인터랙티브 모드."""
    load_dotenv(ENV_PATH, override=True)
    paths_env = os.getenv("PROJECT_PATHS", "")
    input_paths_env = os.getenv("INPUT_PATHS", "")
    
    existing_selected = set()
    if paths_env:
        existing_selected = {p.strip() for p in paths_env.split(",") if p.strip()}
    
    print("\n🔧 Claw-Log 프로젝트 관리")
    print("=" * 50)
    
    if existing_selected:
        print(f"   현재 {len(existing_selected)}개 프로젝트 등록됨")
        for p in sorted(existing_selected):
            print(f"   📂 {Path(p).name} → {p}")
    else:
        print("   등록된 프로젝트 없음")
    
    print("\n   [1] 경로 추가 탐색 (새 경로 입력)")
    print("   [2] 기존 경로 재탐색 (선택/해제 변경)")
    print("   [3] 취소")
    choice = safe_input("   👉 선택 (1/2/3): ", "3").strip()
    
    if choice == "1":
        print("\n   추가할 경로를 입력하세요 (쉼표 구분).")
        new_paths = safe_input("   👉 경로: ", "").strip()
        if not new_paths:
            print("   ⚠️ 경로가 입력되지 않았습니다.")
            return
        
        # 기존 INPUT_PATHS + 새 경로 합치기
        combined_input = input_paths_env
        if combined_input:
            combined_input += "," + new_paths
        else:
            combined_input = new_paths
        
        selected, input_paths = discover_and_select(combined_input, existing_selected)
        if selected:
            _update_env_projects(selected, input_paths)
    
    elif choice == "2":
        if not input_paths_env:
            print("   ⚠️ 탐색할 기존 경로가 없습니다. [1]을 선택하세요.")
            return
        selected, input_paths = discover_and_select(input_paths_env, existing_selected)
        if selected:
            _update_env_projects(selected, input_paths)
    
    else:
        print("   ⏭️ 취소됨.")


def _read_env_data():
    """기존 .env 파일을 dict로 읽어옵니다."""
    env_data = {}
    if ENV_PATH.exists():
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    env_data[key] = value
    return env_data


def _save_env_data(env_data):
    """env_data dict를 .env 파일에 저장합니다."""
    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            for key, value in env_data.items():
                f.write(f"{key}={value}\n")
        return True
    except Exception as e:
        print(f"❌ 설정 저장 실패: {e}")
        return False


def _update_env_projects(selected_paths, input_paths):
    """선택된 프로젝트 경로를 .env에 업데이트합니다."""
    load_dotenv(ENV_PATH, override=True)
    env_data = _read_env_data()
    env_data["PROJECT_PATHS"] = ",".join(selected_paths)
    env_data["INPUT_PATHS"] = input_paths
    if _save_env_data(env_data):
        print(f"✅ 프로젝트 설정 저장 완료: {ENV_PATH.absolute()}")


def show_status():
    """현재 Claw-Log 전체 설정 상태를 한눈에 출력합니다."""
    load_dotenv(ENV_PATH, override=True)

    print("\n📊 Claw-Log 상태")
    print("━" * 40)

    # 엔진 정보
    llm_type = os.getenv("LLM_TYPE", "")
    if not llm_type:
        print(f"  엔진:     ⚠️ 미설정 (claw-log --reset)")
    else:
        engine_label = llm_type.upper()
        if llm_type == "openai-oauth":
            codex_model = os.getenv("CODEX_MODEL", "gpt-5.1")
            engine_label = f"OPENAI-OAUTH / {codex_model}"
        print(f"  엔진:     {engine_label}")

    # 프로젝트 정보
    paths_env = os.getenv("PROJECT_PATHS", "")
    if paths_env:
        paths = [p.strip() for p in paths_env.split(",") if p.strip()]
        valid = sum(1 for p in paths if Path(p).exists())
        print(f"  프로젝트:  {len(paths)}개 등록 ({valid}개 유효)")
    else:
        print(f"  프로젝트:  ⚠️ 미설정")

    # 스케줄 정보
    schedule_info = get_schedule_summary()
    print(f"  스케줄:    {schedule_info}")

    # 로그 파일 정보
    log_path = LOG_FILE
    if log_path.exists():
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                content = f.read()
            line_count = content.count("\n")
            # 최근 날짜 추출
            import re
            dates = re.findall(r"## 📅 (\d{4}-\d{2}-\d{2})", content)
            last_date = dates[0] if dates else "알 수 없음"
            print(f"  로그파일:  career_logs.md ({line_count}줄, 최근: {last_date})")
        except Exception:
            print(f"  로그파일:  career_logs.md (읽기 실패)")
    else:
        print(f"  로그파일:  없음 (첫 실행 전)")

    # 커밋 추적 상태
    try:
        tracking_state = load_state()
        n = len(tracking_state.get("projects", {}))
        if n:
            print(f"  추적상태:  {n}개 프로젝트 커밋 추적 중")
        else:
            print(f"  추적상태:  초기 상태")
    except Exception:
        print(f"  추적상태:  조회 실패")

    print("━" * 40)


# ── 엔진 선택 (공용) ──

def select_engine():
    """AI 엔진 선택 UI. 반환: (llm_type, api_key, codex_model) 또는 실패 시 None."""
    print("\n   사용할 AI 엔진을 선택하세요.")
    print("   [1] Google Gemini (무료 티어 제공)")
    print("   [2] OpenAI GPT-4o-mini (API Key 방식, 종량제)")
    print("   [3] OpenAI Codex (ChatGPT 구독 OAuth 로그인)")
    choice = safe_input("   👉 선택 (1/2/3): ", "").strip()

    if choice == "1":
        llm_type = "gemini"
    elif choice == "3":
        llm_type = "openai-oauth"
    else:
        llm_type = "openai"

    api_key = ""
    codex_model = ""
    if llm_type == "openai-oauth":
        print(f"\n   ChatGPT 계정으로 브라우저 로그인을 진행합니다.")
        print("   ⚠️  ChatGPT Plus 또는 Pro 구독이 필요합니다.")
        print("   ⚠️  구독 요금제의 사용량 제한을 공유합니다.")
        confirm = safe_input("   👉 계속 진행할까요? (y/n): ", "n").strip().lower()
        if confirm != 'y':
            return None

        from claw_log.oauth import run_oauth_login
        token_data = run_oauth_login()
        if not token_data:
            print("❌ OAuth 로그인에 실패했습니다.")
            return None
        api_key = "__OAUTH__"
        print("   ✅ OAuth 로그인 성공!")

        print("\n   🧠 사용할 모델을 선택하세요.")
        print("   [1] GPT-5.1  — 범용 추론, 쿼터 효율적 (추천)")
        print("   [2] GPT-5.2  — 최고 성능, 쿼터 약 1.75배 소모")
        model_choice = safe_input("   👉 선택 (1/2, 기본=1): ", "1").strip()
        if model_choice == "2":
            codex_model = "gpt-5.2"
            print("   ✅ 모델: GPT-5.2 (output 토큰 비용 5.1 대비 1.75배)")
        else:
            codex_model = "gpt-5.1"
            print("   ✅ 모델: GPT-5.1")
    else:
        if llm_type == "gemini":
            print("   (발급: https://aistudio.google.com/app/apikey)")
        else:
            print("   (발급: https://platform.openai.com/api-keys)")

        api_key = safe_input("   👉 API Key: ", "").strip()
        if not api_key:
            print("❌ API Key가 필요합니다.")
            return None

    return llm_type, api_key, codex_model


def change_engine():
    """엔진/모델만 변경합니다 (프로젝트·스케줄 설정 유지)."""
    load_dotenv(ENV_PATH, override=True)
    current = os.getenv("LLM_TYPE", "미설정").upper()
    print(f"\n🔧 AI 엔진 변경 (현재: {current})")

    result = select_engine()
    if result is None:
        print("❌ 엔진 변경이 취소되었습니다.")
        return

    llm_type, api_key, codex_model = result
    env_data = _read_env_data()
    env_data["LLM_TYPE"] = llm_type
    env_data["API_KEY"] = api_key
    if codex_model:
        env_data["CODEX_MODEL"] = codex_model
    elif "CODEX_MODEL" in env_data:
        del env_data["CODEX_MODEL"]

    if _save_env_data(env_data):
        print(f"✅ 엔진 변경 완료: {llm_type.upper()}")


# ── Notion 설정 ──

def setup_notion():
    """Notion 연동 설정. 토큰 입력 → 페이지 검색 → 선택 → DB 생성."""
    import questionary
    from claw_log.notion import NotionClient, NotionAPIError

    print("\n☁️  Notion 연동 설정\n")

    # Step 1: 토큰 생성 안내
    print("   📝 Notion Internal Integration Token을 준비해주세요.")
    print("      1. https://www.notion.so/profile/integrations/internal 접속")
    print("      2. 'Create a new integration' 클릭")
    print("      3. 이름 입력 → 워크스페이스 선택 → Create 클릭")
    print("      4. 생성된 Integration 선택 → 'Internal Integration Token' (secret_...) 복사\n")

    # Step 2: 토큰 입력 + 검증
    while True:
        token = safe_input("   👉 Notion Token: ", "").strip()
        if not token:
            print("   ⚠️  토큰이 입력되지 않았습니다.")
            return False

        print("   🔍 토큰 검증 중...", end=" ")
        client = NotionClient(token)
        if client.validate_token():
            print("✅")
            break
        else:
            print("❌ 유효하지 않은 토큰입니다. 다시 입력해주세요.")

    # Step 3: 연결된 페이지 검색
    while True:
        print("\n   🔍 접근 가능한 페이지를 검색 중...")
        try:
            pages = client.search_pages()
        except NotionAPIError as e:
            print(f"   ❌ 페이지 검색 실패: {e}")
            return False

        if pages:
            break

        print("   ⚠️  Integration에 연결된 페이지가 없습니다.")
        print("      👉 Notion 앱에서 원하는 페이지 열기 → ⋯ → 연결 → Integration 선택")
        retry = safe_input("      👉 연결 후 Enter를 눌러 다시 검색하세요. (q=취소): ", "q").strip()
        if retry.lower() == "q":
            print("   ⏭️  Notion 설정을 건너뜁니다.")
            return False

    # Step 4: 페이지 선택
    choices = []
    for p in pages:
        label = f"{p['title']:<30s}  {p['url']}"
        choices.append(questionary.Choice(title=label, value=p["id"]))

    print(f"\n   🔍 {len(pages)}개 페이지 발견")
    selected_page_id = questionary.select(
        "   Career Logs를 저장할 페이지를 선택하세요:",
        choices=choices,
        instruction="(↑↓ 이동, Enter 확정)",
    ).ask()

    if not selected_page_id:
        print("   ⚠️  선택이 취소되었습니다.")
        return False

    # Step 5: DB 생성
    print("\n   📦 Career Logs Database 생성 중...", end=" ")
    try:
        db_id, ds_id = client.ensure_database(selected_page_id)
        print("✅")
    except NotionAPIError as e:
        print(f"❌\n   {e}")
        if e.hint:
            print(f"   💡 {e.hint}")
        return False

    # .env 저장
    env_data = _read_env_data()
    env_data["NOTION_TOKEN"] = token
    env_data["NOTION_PAGE_ID"] = selected_page_id
    env_data["NOTION_DB_ID"] = db_id
    env_data["NOTION_DS_ID"] = ds_id
    if _save_env_data(env_data):
        print(f"\n   ✅ Notion 연동 완료!")
        return True
    return False


def disconnect_notion():
    """Notion 연동 해제 (.env에서 키 제거)."""
    load_dotenv(ENV_PATH, override=True)
    env_data = _read_env_data()
    removed = False
    for key in ("NOTION_TOKEN", "NOTION_PAGE_ID", "NOTION_DB_ID", "NOTION_DS_ID"):
        if key in env_data:
            del env_data[key]
            removed = True
    if removed:
        _save_env_data(env_data)
        print("✅ Notion 연동이 해제되었습니다.")
    else:
        print("ℹ️  Notion 연동 설정이 없습니다.")


# ── Notion 클라이언트 초기화 (공용) ──

def _init_notion():
    """환경변수에서 Notion 설정을 읽어 클라이언트를 초기화."""
    token = os.getenv("NOTION_TOKEN", "")
    page_id = os.getenv("NOTION_PAGE_ID", "")
    db_id = os.getenv("NOTION_DB_ID", "")
    ds_id = os.getenv("NOTION_DS_ID", "")
    if not token:
        return None, None, None, None
    from claw_log.notion import NotionClient
    client = NotionClient(token)
    return client, db_id, ds_id, page_id


# ── 마법사 ──

def run_wizard():
    print("\n🔮 Claw-Log 초기 설정 마법사 (Tri-LLM Edition)\n")

    print("1️⃣  사용할 AI 엔진을 선택하세요.")
    result = select_engine()
    if result is None:
        print("❌ 설정이 취소되었습니다.")
        sys.exit(1)
    llm_type, api_key, codex_model = result

    # 3. 프로젝트 경로 (토글 선택)
    print("\n3️⃣  분석할 Git 프로젝트 경로들을 입력하세요 (쉼표 구분).")
    print("   💡 상위 폴더를 입력하면 하위 Git 프로젝트를 자동 탐색합니다.")
    print("   💡 직접 지정한 Git 프로젝트 → 자동 선택")
    print("   💡 하위에서 발견된 프로젝트 → 수동 선택")
    print("   (예시: /Users/kim/workspace,/Users/kim/side-project)")
    paths_input = safe_input("   👉 경로: ", "").strip()
    
    selected_paths, input_paths = discover_and_select(paths_input)
    if not selected_paths:
        print("❌ 최소 1개 이상의 프로젝트를 선택해야 합니다.")
        sys.exit(1)
    
    # .env 저장
    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write(f"LLM_TYPE={llm_type}\n")
            f.write(f"API_KEY={api_key}\n")
            f.write(f"PROJECT_PATHS={','.join(selected_paths)}\n")
            f.write(f"INPUT_PATHS={input_paths}\n")
            if codex_model:
                f.write(f"CODEX_MODEL={codex_model}\n")
        print(f"\n✅ 설정 저장 완료: {ENV_PATH.absolute()}")
    except Exception as e:
        print(f"❌ 설정 저장 실패: {e}")
        sys.exit(1)

    # 4. 스케줄
    print("\n4️⃣  매일 자동 기록 스케줄을 등록할까요?")
    print("   실행 시각을 입력하세요 (예: 23:30, 18:00).")
    print("   등록하지 않으려면 그냥 Enter를 누르세요.")
    schedule_time = safe_input("   👉 시각 (HH:MM): ", "").strip()
    
    if schedule_time:
        import re
        if re.match(r"^\d{1,2}:\d{2}$", schedule_time):
            h, m = schedule_time.split(":")
            if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
                install_schedule(schedule_time)
            else:
                print("   ⚠️ 유효하지 않은 시각입니다. 스케줄 등록을 건너뜁니다.")
        else:
            print("   ⚠️ HH:MM 형식이 아닙니다. 스케줄 등록을 건너뜁니다.")
    else:
        print("   ⏭️  자동 기록 스케줄을 건너뜁니다.")

    # 5. Notion 연동 (선택사항)
    print("\n5️⃣  Notion 연동을 설정할까요? (선택사항)")
    print("   [1] 설정하기")
    print("   [2] 나중에 (건너뛰기)")
    notion_choice = safe_input("   👉 선택 (1/2): ", "2").strip()
    if notion_choice == "1":
        setup_notion()
    else:
        print("   ⏭️  Notion 연동을 건너뜁니다.")


# ── 배칭 파이프라인 ──

BATCH_CHAR_LIMIT = 50_000


def format_commit_for_batch(commit, project_name):
    """커밋 데이터를 배치 텍스트 블록으로 포맷.

    Args:
        commit: CommitData 인스턴스
        project_name: 프로젝트 이름

    Returns:
        포맷된 문자열
    """
    short_hash = commit.hash[:8]
    parts = [f"--- [{project_name}] commit {short_hash} ---"]
    if commit.stat:
        parts.append(f"[STAT]\n{commit.stat}")
    if commit.diff:
        parts.append(f"[DIFF]\n{commit.diff}")
    return "\n".join(parts) + "\n"


def format_uncommitted_for_batch(stat, diff, project_name):
    """미커밋 변경사항을 배치 텍스트 블록으로 포맷.

    Args:
        stat: git diff --stat 출력
        diff: git diff 출력
        project_name: 프로젝트 이름

    Returns:
        포맷된 문자열
    """
    parts = [f"--- [{project_name}] uncommitted changes ---"]
    if stat:
        parts.append(f"[STAT]\n{stat}")
    if diff:
        parts.append(f"[DIFF]\n{diff}")
    return "\n".join(parts) + "\n"


def _flush_batch(batch_text, batch_commits, summarizer, days, batch_idx):
    """배치를 AI에 전송하고 결과를 반환.

    Args:
        batch_text: AI에 전송할 텍스트
        batch_commits: 이 배치에 포함된 (repo_key, commit_hash) 튜플 리스트
        summarizer: AI 요약 엔진
        days: --days 값
        batch_idx: 배치 번호 (로깅용)

    Returns:
        (success, summary_text) 튜플
    """
    batch_label = f"배치 #{batch_idx + 1}"
    print(f"  {batch_label} AI 요약 생성 중... ({len(batch_text):,}자)")
    summary = summarizer.summarize(batch_text)

    if not summary or summary.startswith(("Gemini 요약 생성 실패", "OpenAI 요약 생성 실패")):
        print(f"  {batch_label} 요약 실패: {summary}")
        return (False, "")

    # 점진적 상태 저장 (days==0일 때만)
    if days == 0 and batch_commits:
        pending = {}
        for repo_key, commit_hash in batch_commits:
            pending[repo_key] = commit_hash  # 같은 repo_key의 마지막 해시가 남음
        if pending:
            save_state(pending)

    return (True, summary)


def run_batched_summarization(target_paths, summarizer, days):
    """배칭 파이프라인으로 모든 프로젝트를 처리.

    Args:
        target_paths: 프로젝트 경로 리스트
        summarizer: AI 요약 엔진
        days: --days 값

    Returns:
        성공 시 True, 실패 시 False
    """
    state = load_state()

    # 1. 모든 프로젝트의 커밋 데이터 수집
    all_projects = []
    for repo_path_str in target_paths:
        repo_key = get_repo_key(repo_path_str)
        last_hash = get_last_hash(state, repo_key) if days == 0 else None
        pc = collect_project_commits(repo_path_str, repo_key, last_hash=last_hash, days=days)
        all_projects.append(pc)

        p_name = pc.project_name
        n_commits = len(pc.commits)
        has_uncommitted = pc.uncommitted_diff is not None
        if n_commits > 0 or has_uncommitted:
            parts = []
            if n_commits > 0:
                parts.append(f"커밋 {n_commits}개")
            if has_uncommitted:
                parts.append("미커밋 변경")
            print(f"  [{p_name}] {', '.join(parts)} 수집 완료")
        elif Path(repo_path_str).exists():
            no_change = f"최근 {days}일 변경사항 없음" if days > 0 else "오늘 변경사항 없음"
            print(f"  [{p_name}] {no_change}")

    # 2. 배치 구성 및 AI 호출
    batch_text = ""
    batch_commits = []  # (repo_key, last_hash_in_batch) 추적용
    all_summaries = []
    batch_idx = 0
    has_any_data = False

    for pc in all_projects:
        # 커밋 처리
        for commit in pc.commits:
            has_any_data = True
            chunk = format_commit_for_batch(commit, pc.project_name)

            # 배치 한도 초과 시 flush
            if batch_text and len(batch_text) + len(chunk) > BATCH_CHAR_LIMIT:
                ok, summary = _flush_batch(batch_text, batch_commits, summarizer, days, batch_idx)
                if not ok:
                    if all_summaries:
                        _save_all_summaries(all_summaries, days)
                    return False
                all_summaries.append(summary)
                batch_text = ""
                batch_commits = []
                batch_idx += 1

            batch_text += chunk
            batch_commits.append((pc.repo_key, commit.hash))

        # 미커밋 변경사항 처리
        if pc.uncommitted_diff:
            has_any_data = True
            chunk = format_uncommitted_for_batch(pc.uncommitted_stat, pc.uncommitted_diff, pc.project_name)

            if batch_text and len(batch_text) + len(chunk) > BATCH_CHAR_LIMIT:
                ok, summary = _flush_batch(batch_text, batch_commits, summarizer, days, batch_idx)
                if not ok:
                    if all_summaries:
                        _save_all_summaries(all_summaries, days)
                    return False
                all_summaries.append(summary)
                batch_text = ""
                batch_commits = []
                batch_idx += 1

            batch_text += chunk

    if not has_any_data:
        print("변경사항이 발견되지 않았습니다. (종료)")
        return False

    # 마지막 배치 flush
    if batch_text:
        ok, summary = _flush_batch(batch_text, batch_commits, summarizer, days, batch_idx)
        if not ok:
            if all_summaries:
                _save_all_summaries(all_summaries, days)
            return False
        all_summaries.append(summary)

    # 3. 모든 요약을 합쳐서 로그에 저장
    if all_summaries:
        _save_all_summaries(all_summaries, days)
        combined = "\n\n".join(all_summaries)
        print(f"\n" + "=" * 60 + f"\n{combined}\n" + "=" * 60)

    return True


def _save_all_summaries(summaries, days):
    """모든 배치 요약을 합쳐서 저장 (Notion + 로컬)."""
    combined = "\n\n".join(summaries)
    if days > 0:
        start_date = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
        end_date = datetime.date.today().strftime("%Y-%m-%d")
        date_label = f"{start_date} ~ {end_date}"
    else:
        date_label = None

    notion_client, notion_db_id, notion_ds_id, notion_page_id = _init_notion()

    # DB ID / DS ID 확보 (Notion 설정이 있는 경우)
    if notion_client and notion_page_id and not notion_ds_id:
        try:
            from claw_log.notion import NotionAPIError
            notion_db_id, notion_ds_id = notion_client.ensure_database(
                notion_page_id, database_id=notion_db_id
            )
            # DB ID / DS ID 캐싱
            env_data = _read_env_data()
            env_data["NOTION_DB_ID"] = notion_db_id
            env_data["NOTION_DS_ID"] = notion_ds_id
            _save_env_data(env_data)
        except Exception:
            pass

    result = save_log(
        combined,
        date_label=date_label,
        notion_client=notion_client,
        notion_ds_id=notion_ds_id,
        notion_page_id=notion_page_id,
    )

    if result["notion"]:
        print(f"\n☁️  Notion 저장 완료: {result['notion_url']}")
    if result["local"]:
        print(f"💾 로컬 저장 완료: {result['local_path']}")
    if result.get("error"):
        print(f"⚠️  {result['error']}")


# ── 업데이트 확인 ──

def check_and_update():
    """PyPI에서 최신 버전을 확인하고 업데이트 여부를 사용자에게 묻습니다."""
    if not sys.stdin.isatty():
        return  # 비대화형 환경에서는 업데이트 확인 스킵
    import urllib.request
    import json

    print("🔍 최신 버전 확인 중...")
    try:
        url = "https://pypi.org/pypi/claw-log-sh/json"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        latest = data["info"]["version"]
    except Exception as e:
        print(f"⚠️ 버전 확인 실패: {e}")
        return

    current = __version__

    def _ver_tuple(v):
        return tuple(int(x) for x in v.split(".") if x.isdigit())

    print(f"  현재 버전: {current}")
    print(f"  최신 버전: {latest}")

    if current != "unknown" and _ver_tuple(latest) <= _ver_tuple(current):
        print("✅ 최신 버전입니다.")
        return

    print()
    confirm = safe_input("   업데이트할까요? (y/n): ", "n").strip().lower()
    if confirm == "y":
        print("📦 업데이트 중...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "claw-log-sh"]
        )
        if result.returncode == 0:
            print("✅ 업데이트 완료! 변경 적용을 위해 다시 실행해주세요.")
        else:
            print("❌ 업데이트 실패. 직접 실행해주세요:")
            print("   pip install --upgrade claw-log-sh")
    else:
        print("  ⏭️ 업데이트를 건너뜁니다.")


# ── 환경 점검 ──

def check_environment():
    """실행 전 필수 의존성 및 환경 점검"""
    try:
        import google.genai
        import openai
        import dotenv
    except ImportError as e:
        print(f"❌ [Critical Error] 필수 라이브러리가 설치되지 않았습니다: {e}")
        print("   👉 'pip install claw-log --force-reinstall'을 시도해보세요.")
        sys.exit(1)


# ── 메인 ──

def main():
    # Windows 스케줄러(cmd /c) 등 non-UTF-8 환경에서 이모지 출력 크래시 방지
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Claw-Log: 커리어 자동 기록 도구")
    parser.add_argument("--version", action="version", version=f"claw-log {__version__}")
    parser.add_argument("--reset", action="store_true", help="설정 초기화 및 마법사 재실행")
    parser.add_argument("--schedule", metavar="HH:MM", help="스케줄 등록/변경 (예: --schedule 23:30)")
    parser.add_argument("--schedule-show", action="store_true", help="현재 스케줄 조회")
    parser.add_argument("--schedule-remove", action="store_true", help="스케줄 삭제")
    parser.add_argument("--projects", action="store_true", help="프로젝트 관리 (추가/선택/해제)")
    parser.add_argument("--projects-show", action="store_true", help="현재 프로젝트 목록 조회")
    parser.add_argument("--status", action="store_true", help="전체 설정 상태 조회")
    parser.add_argument("--dry-run", action="store_true", help="API 호출 없이 수집될 diff 미리보기")
    parser.add_argument("--engine", action="store_true", help="AI 엔진/모델 변경 (프로젝트·스케줄 유지)")
    parser.add_argument("--days", type=int, default=0, metavar="N", help="과거 N일치 커밋 요약 (예: --days 7)")
    parser.add_argument("--log", nargs="?", const=5, type=int, metavar="N", help="최근 N개 로그 조회 (기본: 5)")
    parser.add_argument("--serve", nargs="?", const=8080, type=int, metavar="PORT", help="로컬 웹 대시보드 (기본 포트: 8080)")
    parser.add_argument("--log-edit", action="store_true", help="커리어 로그 파일을 기본 편집기로 열기")
    parser.add_argument("--update", action="store_true", help="최신 버전 확인 및 업데이트")
    parser.add_argument("--notion-setup", action="store_true", help="Notion 연동 설정")
    parser.add_argument("--notion-disconnect", action="store_true", help="Notion 연동 해제")
    args = parser.parse_args()

    # 0. 즉시 실행 명령어 (설정 불필요)
    if args.update:
        check_and_update()
        return
    if args.serve is not None:
        from claw_log.server import serve_dashboard
        serve_dashboard(port=args.serve)
        return
    if args.status:
        show_status()
        return
    if args.engine:
        change_engine()
        return
    if args.notion_setup:
        load_dotenv(ENV_PATH, override=True)
        setup_notion()
        return
    if args.notion_disconnect:
        disconnect_notion()
        return
    if args.log_edit:
        log_path = LOG_FILE
        if not log_path.exists():
            print("⚠️ 로그 파일이 없습니다. 먼저 'claw-log'를 실행하세요.")
            return
        import platform
        system = platform.system()
        if system == "Windows":
            os.startfile(log_path)
        elif system == "Darwin":
            subprocess.run(["open", str(log_path)])
        else:
            try:
                subprocess.run(["xdg-open", str(log_path)])
            except FileNotFoundError:
                print(f"⚠️ 편집기를 열 수 없습니다. 직접 열어주세요: {log_path}")
                return
        print(f"📝 편집기로 열었습니다: {log_path}")
        return
    if args.log is not None:
        entries, error = read_recent_logs(n=args.log)
        if error:
            print(f"⚠️ {error}")
        else:
            print(f"\n📋 최근 {len(entries)}개 기록\n")
            for entry in entries:
                print(entry)
                print("\n" + "─" * 50 + "\n")
        return
    if args.schedule_show:
        show_schedule()
        return
    if args.schedule_remove:
        remove_schedule()
        return
    if args.projects_show:
        show_projects()
        return
    if args.projects:
        manage_projects()
        return

    # dry-run은 환경 점검/API 설정 없이 diff만 수집
    if args.dry_run:
        load_dotenv(ENV_PATH, override=True)
        paths_env = os.getenv("PROJECT_PATHS", "")
        if not paths_env:
            print("❌ 프로젝트가 설정되지 않았습니다. 'claw-log' 명령으로 먼저 설정하세요.")
            return

        target_paths = [p.strip() for p in paths_env.split(",") if p.strip()]
        days = args.days
        print(f"\n🔍 Claw-Log Dry Run — {len(target_paths)}개 프로젝트 스캔")
        if days > 0:
            print(f"   (과거 {days}일치 모드)")
        print("=" * 50)

        dry_state = load_state()
        total_chars = 0
        total_commits = 0
        collected = 0

        # 배치 시뮬레이션용
        simulated_batch_size = 0
        estimated_batches = 1

        for repo_path_str in target_paths:
            p_name = Path(repo_path_str).name
            repo_key = get_repo_key(repo_path_str)
            last_hash = get_last_hash(dry_state, repo_key) if days == 0 else None

            pc = collect_project_commits(repo_path_str, repo_key, last_hash=last_hash, days=days)
            n_commits = len(pc.commits)
            has_uncommitted = pc.uncommitted_diff is not None

            if n_commits > 0 or has_uncommitted:
                collected += 1
                total_commits += n_commits

                # 프로젝트별 문자 수 계산
                proj_chars = 0
                for c in pc.commits:
                    chunk = format_commit_for_batch(c, p_name)
                    proj_chars += len(chunk)
                    simulated_batch_size += len(chunk)
                    if simulated_batch_size > BATCH_CHAR_LIMIT:
                        estimated_batches += 1
                        simulated_batch_size = len(chunk)

                if has_uncommitted:
                    chunk = format_uncommitted_for_batch(pc.uncommitted_stat, pc.uncommitted_diff, p_name)
                    proj_chars += len(chunk)
                    simulated_batch_size += len(chunk)
                    if simulated_batch_size > BATCH_CHAR_LIMIT:
                        estimated_batches += 1
                        simulated_batch_size = len(chunk)

                total_chars += proj_chars
                parts = []
                if n_commits > 0:
                    parts.append(f"커밋 {n_commits}개")
                if has_uncommitted:
                    parts.append("미커밋 변경")
                print(f"  [{p_name}] {', '.join(parts)} — {proj_chars:,}자")
            elif Path(repo_path_str).exists():
                print(f"  [{p_name}] 변경사항 없음")
            else:
                print(f"  [{p_name}] 경로 없음")

        print("=" * 50)
        print(f"  수집 프로젝트:     {collected}/{len(target_paths)}")
        print(f"  총 커밋 수:        {total_commits}개")
        print(f"  총 데이터 크기:    {total_chars:,}자 (약 {total_chars // 4:,} 토큰)")
        print(f"  예상 AI 호출 횟수: {estimated_batches}회 (배치 한도: {BATCH_CHAR_LIMIT:,}자)")
        if total_chars == 0:
            print("  ⚠️ 변경사항이 없습니다.")
        return

    # 단일 인스턴스 보호 — 위자드 및 실제 실행을 동시에 두 번 돌리지 않도록 차단
    import atexit
    run_lock_err = acquire_run_lock()
    if run_lock_err:
        print(f"❌ {run_lock_err}")
        return
    atexit.register(release_run_lock)

    # 0-1. 런타임 환경 점검 (Pre-flight Check)
    check_environment()

    # 1. Reset 요청 시 기존 설정 파일 삭제
    if args.reset and ENV_PATH.exists():
        try:
            ENV_PATH.unlink()
            print("🔄 기존 설정을 초기화했습니다.")
        except Exception as e:
            print(f"⚠️ 설정 파일 삭제 실패: {e}")

    # 2. 환경변수 로드
    load_dotenv(ENV_PATH, override=True)

    required_vars_missing = not os.getenv("API_KEY") or not os.getenv("LLM_TYPE")
    should_run_wizard = args.reset or not ENV_PATH.exists() or required_vars_missing

    if should_run_wizard:
        run_wizard()
        load_dotenv(ENV_PATH, override=True)

    # 3. 스케줄 등록/변경
    if args.schedule:
        import re
        if re.match(r"^\d{1,2}:\d{2}$", args.schedule):
            h, m = args.schedule.split(":")
            if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
                install_schedule(args.schedule)
            else:
                print("❌ 유효하지 않은 시각입니다. (예: --schedule 23:30)")
        else:
            print("❌ HH:MM 형식으로 입력하세요. (예: --schedule 23:30)")
        return

    # 4. 설정 로드 및 검증
    llm_type = os.getenv("LLM_TYPE", "gemini").lower()
    api_key = os.getenv("API_KEY")
    paths_env = os.getenv("PROJECT_PATHS", "")

    if not api_key:
        print("❌ API Key가 설정되지 않았습니다. 마법사를 완료하거나 .env 파일을 확인해주세요.")
        return

    # Summarizer 초기화
    summarizer = None
    if llm_type == "openai-oauth":
        codex_model = os.getenv("CODEX_MODEL", "gpt-5.1")
        summarizer = CodexOAuthSummarizer(model=codex_model)
    elif llm_type == "openai":
        summarizer = OpenAISummarizer(api_key)
    else:
        summarizer = GeminiSummarizer(api_key)

    engine_label = llm_type.upper()
    if llm_type == "openai-oauth":
        engine_label = f"OPENAI-OAUTH / {codex_model}"
    days = args.days
    if days > 0:
        print(f"🚀 Claw-Log 분석 시작 — 과거 {days}일 (Engine: {engine_label})...")
    else:
        print(f"🚀 Claw-Log 분석 시작 (Engine: {engine_label})...")

    # 5. 배칭 파이프라인으로 Git 데이터 수집 + AI 요약
    target_paths = [p.strip() for p in paths_env.split(",") if p.strip()]
    run_batched_summarization(target_paths, summarizer, days)

if __name__ == "__main__":
    main()