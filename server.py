"""
Claw-Log Dashboard Server
로컬 웹 대시보드: 설정, 프로젝트, 스케줄, 커리어 로그를 브라우저에서 읽기 전용 조회.
"""

import json
import re
import webbrowser
from html import escape
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# NOTE: main.py에서 lazy import로 server를 호출하므로 circular import 없음.
# _read_env_data/ENV_PATH를 별도 config 모듈로 분리하면 더 안전함. (TODO)
from claw_log.main import _read_env_data, ENV_PATH
from claw_log.storage import read_recent_logs
from claw_log.scheduler import get_schedule_summary


# ── 데이터 수집 ──

def _collect_dashboard_data():
    """대시보드에 표시할 데이터를 수집합니다."""
    env_data = _read_env_data()

    # 설정
    llm_type = env_data.get("LLM_TYPE", "")
    api_key_raw = env_data.get("API_KEY", "")
    codex_model = env_data.get("CODEX_MODEL", "")

    if api_key_raw and api_key_raw != "__OAUTH__":
        api_key_display = api_key_raw[:4] + "****" if len(api_key_raw) > 4 else "****"
    elif api_key_raw == "__OAUTH__":
        api_key_display = "OAuth 인증"
    else:
        api_key_display = "미설정"

    engine_label = llm_type.upper() if llm_type else "미설정"
    if llm_type == "openai-oauth" and codex_model:
        engine_label = f"OPENAI-OAUTH / {codex_model}"

    settings = {
        "engine": engine_label,
        "api_key": api_key_display,
        "llm_type": llm_type,
    }

    # 프로젝트
    paths_env = env_data.get("PROJECT_PATHS", "")
    projects = []
    if paths_env:
        for p in paths_env.split(","):
            p = p.strip()
            if p:
                path = Path(p)
                projects.append({
                    "name": path.name,
                    "path": p,
                    "exists": path.exists(),
                    "has_git": (path / ".git").exists() if path.exists() else False,
                })

    # 스케줄
    schedule = get_schedule_summary()

    # 로그
    entries, error = read_recent_logs(n=20)
    logs = entries if entries else []
    log_error = error

    # Notion
    notion_token = env_data.get("NOTION_TOKEN", "")
    notion_db_id = env_data.get("NOTION_DB_ID", "")
    notion_page_id = env_data.get("NOTION_PAGE_ID", "")
    notion_status = {
        "connected": bool(notion_token and notion_db_id),
        "page_id": notion_page_id or "미설정",
    }

    return {
        "settings": settings,
        "projects": projects,
        "schedule": schedule,
        "logs": logs,
        "log_error": log_error,
        "notion": notion_status,
    }


# ── 마크다운 → HTML 변환 ──

def _md_to_html(md_text):
    """간이 마크다운 → HTML 변환 (career_logs.md 렌더링용)."""
    if not md_text:
        return ""

    lines = md_text.split("\n")
    html_lines = []
    in_list = False

    for line in lines:
        stripped = line.strip()

        # 빈 줄
        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("")
            continue

        # 구분선
        if re.match(r"^-{3,}$", stripped):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("<hr>")
            continue

        # 제목
        h_match = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if h_match:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            level = len(h_match.group(1))
            text = _inline_format(escape(h_match.group(2)))
            html_lines.append(f"<h{level}>{text}</h{level}>")
            continue

        # 인용
        if stripped.startswith(">"):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            text = _inline_format(escape(stripped[1:].strip()))
            html_lines.append(f'<blockquote>{text}</blockquote>')
            continue

        # 리스트
        if re.match(r"^[-*]\s+", stripped):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            text = _inline_format(escape(re.sub(r"^[-*]\s+", "", stripped)))
            html_lines.append(f"<li>{text}</li>")
            continue

        # 일반 텍스트
        if in_list:
            html_lines.append("</ul>")
            in_list = False
        text = _inline_format(escape(stripped))
        html_lines.append(f"<p>{text}</p>")

    if in_list:
        html_lines.append("</ul>")

    return "\n".join(html_lines)


def _inline_format(text):
    """인라인 마크다운 포맷 변환 (볼드, 코드)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


# ── HTML 렌더링 ──

def _render_html(data):
    """대시보드 HTML을 생성합니다."""
    settings = data["settings"]
    projects = data["projects"]
    schedule = data["schedule"]
    logs = data["logs"]
    log_error = data.get("log_error")
    notion = data.get("notion", {})

    # 프로젝트 행
    project_rows = ""
    if projects:
        for i, p in enumerate(projects, 1):
            status = "✅" if p["exists"] and p["has_git"] else ("❌ 경로 없음" if not p["exists"] else "⚠️ .git 없음")
            name = escape(p["name"])
            path = escape(p["path"])
            project_rows += f"<tr><td>{i}</td><td><strong>{name}</strong></td><td class='path'>{path}</td><td>{status}</td></tr>\n"
    else:
        project_rows = "<tr><td colspan='4' class='empty'>등록된 프로젝트가 없습니다.</td></tr>"

    # 로그 섹션
    if log_error:
        logs_html = f"<p class='empty'>{escape(log_error)}</p>"
    elif logs:
        logs_html = ""
        for entry in logs:
            logs_html += f'<div class="log-entry">{_md_to_html(entry)}</div>\n'
    else:
        logs_html = "<p class='empty'>로그가 없습니다.</p>"

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claw-Log Dashboard</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f5f5; color: #333; line-height: 1.6;
    max-width: 960px; margin: 0 auto; padding: 20px;
  }}
  header {{
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    color: white; padding: 24px 32px; border-radius: 12px; margin-bottom: 24px;
  }}
  header h1 {{ font-size: 1.6em; margin-bottom: 4px; }}
  header p {{ opacity: 0.85; font-size: 0.9em; }}
  .card {{
    background: white; border-radius: 10px; padding: 24px;
    margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }}
  .card h2 {{
    font-size: 1.15em; margin-bottom: 16px; padding-bottom: 8px;
    border-bottom: 2px solid #eee;
  }}
  .settings-grid {{
    display: grid; grid-template-columns: 120px 1fr;
    gap: 8px 16px; font-size: 0.95em;
  }}
  .settings-grid .label {{ color: #666; font-weight: 500; }}
  .settings-grid .value {{ font-weight: 600; }}
  table {{
    width: 100%; border-collapse: collapse; font-size: 0.9em;
  }}
  th, td {{ padding: 10px 12px; text-align: left; }}
  th {{ background: #f8f9fa; font-weight: 600; border-bottom: 2px solid #e9ecef; }}
  td {{ border-bottom: 1px solid #f0f0f0; }}
  td.path {{ font-family: monospace; font-size: 0.85em; color: #555; word-break: break-all; }}
  .empty {{ color: #999; font-style: italic; padding: 16px 0; }}
  .schedule-badge {{
    display: inline-block; padding: 6px 14px; border-radius: 20px;
    font-weight: 500; font-size: 0.95em;
  }}
  .schedule-active {{ background: #d4edda; color: #155724; }}
  .schedule-inactive {{ background: #fff3cd; color: #856404; }}
  .log-entry {{
    padding: 16px; margin-bottom: 12px; background: #fafbfc;
    border-radius: 8px; border-left: 3px solid #667eea;
  }}
  .log-entry h2 {{ font-size: 1.05em; color: #333; border: none; padding: 0; margin-bottom: 8px; }}
  .log-entry h3 {{ font-size: 0.95em; color: #555; margin: 8px 0 4px; }}
  .log-entry ul {{ padding-left: 20px; margin: 4px 0; }}
  .log-entry li {{ margin: 2px 0; font-size: 0.9em; }}
  .log-entry p {{ font-size: 0.9em; margin: 4px 0; }}
  .log-entry hr {{ border: none; border-top: 1px solid #e9ecef; margin: 12px 0; }}
  .log-entry blockquote {{ border-left: 3px solid #ddd; padding-left: 12px; color: #666; margin: 8px 0; }}
  .log-entry code {{ background: #e9ecef; padding: 1px 5px; border-radius: 3px; font-size: 0.9em; }}
  .log-entry strong {{ color: #222; }}
  footer {{
    text-align: center; color: #aaa; font-size: 0.8em;
    padding: 16px 0; margin-top: 8px;
  }}
  @media (max-width: 600px) {{
    body {{ padding: 12px; }}
    header {{ padding: 16px 20px; }}
    .card {{ padding: 16px; }}
    .settings-grid {{ grid-template-columns: 1fr; }}
    td.path {{ font-size: 0.75em; }}
  }}
</style>
</head>
<body>
<header>
  <h1>🦞 Claw-Log Dashboard</h1>
  <p>커리어 자동 기록 도구 &mdash; 읽기 전용 대시보드</p>
</header>

<div class="card">
  <h2>⚙️ 설정</h2>
  <div class="settings-grid">
    <span class="label">AI 엔진</span>
    <span class="value">{escape(settings['engine'])}</span>
    <span class="label">API Key</span>
    <span class="value">{escape(settings['api_key'])}</span>
    <span class="label">Notion</span>
    <span class="value">{'<span style="color:#155724">&#x2705; 연결됨</span>' if notion.get('connected') else '<span style="color:#666">&#x26AA; 미연결 (claw-log --notion-setup)</span>'}</span>
  </div>
</div>

<div class="card">
  <h2>📂 프로젝트 ({len(projects)}개)</h2>
  <table>
    <thead><tr><th>#</th><th>이름</th><th>경로</th><th>상태</th></tr></thead>
    <tbody>{project_rows}</tbody>
  </table>
</div>

<div class="card">
  <h2>⏰ 스케줄</h2>
  <span class="schedule-badge {'schedule-inactive' if '\u26a0\ufe0f' in schedule else 'schedule-active'}">
    {escape(schedule)}
  </span>
</div>

<div class="card">
  <h2>📋 커리어 로그 (최근 {len(logs)}건)</h2>
  {logs_html}
</div>

<footer>Claw-Log &bull; localhost 전용 &bull; F5로 새로고침</footer>
</body>
</html>"""


# ── HTTP 핸들러 ──

class DashboardHandler(BaseHTTPRequestHandler):
    """로컬 대시보드 HTTP 핸들러."""

    def do_GET(self):
        if self.path == "/" or self.path == "":
            data = _collect_dashboard_data()
            html = _render_html(data)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/api/data":
            data = _collect_dashboard_data()
            # logs를 직렬화 가능하게 변환
            payload = json.dumps(data, ensure_ascii=False, indent=2, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def log_message(self, format, *args):
        """HTTP 서버 로그 억제."""
        pass


# ── 서버 실행 ──

def serve_dashboard(port=8080):
    """로컬 대시보드 서버를 시작합니다."""
    try:
        server = HTTPServer(("localhost", port), DashboardHandler)
    except OSError:
        print(f"\n❌ 포트 {port}이 이미 사용 중입니다.")
        print(f"   다른 포트를 지정하세요: claw-log --serve {port + 1}")
        return
    url = f"http://localhost:{port}"

    print(f"\n🦞 Claw-Log 대시보드 서버 시작")
    print(f"   📍 {url}")
    print(f"   종료: Ctrl+C\n")

    webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\n👋 대시보드 서버를 종료합니다.")
    finally:
        server.server_close()
