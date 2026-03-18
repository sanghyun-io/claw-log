"""
Claw-Log State Module
커밋 추적 상태를 관리하는 모듈. Atomic write + 단일 인스턴스 PID 락으로 안전하게 저장.
"""

import json
import os
import datetime
import time
from pathlib import Path

# oauth.py:30 패턴 재사용
STATE_DIR = Path.home() / ".claw-log"
STATE_FILE = STATE_DIR / "commit_state.json"
RUN_LOCK_FILE = STATE_DIR / "claw_log.pid"  # 단일 인스턴스 보호용 PID 파일


def _migrate_v1_to_v2(data):
    """v1(브랜치별 키) → v2(repo 단위 키 + processed_commits) 마이그레이션."""
    if data.get("version", 1) >= 2:
        return data

    old_projects = data.get("projects", {})
    new_projects = {}

    for old_key, val in old_projects.items():
        # "repo_root::refs/heads/branch" → "repo_root"
        repo_root = old_key.split("::")[0] if "::" in old_key else old_key

        existing = new_projects.get(repo_root)
        if not existing or val.get("last_run_at", "") > existing.get("last_run_at", ""):
            new_projects[repo_root] = val

    data["version"] = 2
    data["projects"] = new_projects
    return data


def load_state():
    """상태 파일을 로드. 파일 없으면 빈 구조체 반환. 손상 시 .bak 백업 + 경고."""
    if not STATE_FILE.exists():
        return {"version": 2, "projects": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "projects" not in data:
            raise ValueError("Invalid state structure")
        # v1 → v2 자동 마이그레이션
        if data.get("version", 1) < 2:
            data = _migrate_v1_to_v2(data)
            _atomic_save(data)
        return data
    except (json.JSONDecodeError, ValueError):
        # .bak 백업 + 경고
        bak = STATE_FILE.with_suffix(f".bak.{int(time.time())}")
        try:
            os.replace(str(STATE_FILE), str(bak))
            print(f"  ⚠️  상태 파일 손상 → 백업: {bak}")
        except OSError:
            pass
        return {"version": 2, "projects": {}}


def _atomic_save(data):
    """상태 데이터를 파일에 원자적으로 저장."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_FILE.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp_path), str(STATE_FILE))


PROCESSED_COMMITS_TTL_DAYS = 90


def save_state(pending_hashes, batch_commit_hashes=None):
    """pending_hashes를 상태 파일에 원자적으로 병합 저장.

    Args:
        pending_hashes: {repo_key: commit_hash} 딕셔너리 — 이번 배치의 마지막 커밋 해시
        batch_commit_hashes: {repo_key: [hash, ...]} 딕셔너리 — 이번 배치에서 처리된 모든 커밋 해시
    """
    current = load_state()
    now = datetime.datetime.now().isoformat()
    now_date = datetime.datetime.now().strftime("%Y-%m-%d")
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=PROCESSED_COMMITS_TTL_DAYS)).strftime("%Y-%m-%d")

    for repo_key, commit_hash in pending_hashes.items():
        proj = current["projects"].setdefault(repo_key, {})
        proj["last_commit_hash"] = commit_hash
        proj["last_run_at"] = now

        # processed_commits 누적
        pc = proj.setdefault("processed_commits", {})
        if batch_commit_hashes and repo_key in batch_commit_hashes:
            for h in batch_commit_hashes[repo_key]:
                pc[h] = now_date
        else:
            pc[commit_hash] = now_date

        # TTL pruning
        proj["processed_commits"] = {h: d for h, d in pc.items() if d >= cutoff}

    _atomic_save(current)


def get_last_hash(state, repo_key):
    """repo_key에 해당하는 마지막 처리 커밋 해시 반환. 없으면 None."""
    entry = state.get("projects", {}).get(repo_key)
    return entry.get("last_commit_hash") if entry else None


def get_processed_commits(state, repo_key):
    """repo_key의 처리 완료 커밋 해시 set 반환."""
    entry = state.get("projects", {}).get(repo_key, {})
    return set(entry.get("processed_commits", {}).keys())


def save_failure_since(date_str):
    """AI 요약 실패 시작 날짜를 state에 기록 (이미 있으면 덮어쓰지 않음).

    Args:
        date_str: 실패 시작 날짜 (YYYY-MM-DD)
    """
    current = load_state()
    if "failure_since" not in current:
        current["failure_since"] = date_str
        _atomic_save(current)


def get_failure_since(state):
    """state에서 failure_since 날짜 반환. 없으면 None.

    Args:
        state: load_state()로 로드한 state dict
    Returns:
        str | None — 실패 시작 날짜 (YYYY-MM-DD) 또는 None
    """
    return state.get("failure_since")


def clear_failure_since():
    """state에서 failure_since를 제거 (복구 완료 후 호출)."""
    current = load_state()
    if "failure_since" in current:
        del current["failure_since"]
        _atomic_save(current)


def acquire_run_lock():
    """단일 인스턴스 보장. 이미 실행 중이면 에러 메시지 반환, 성공 시 None 반환."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(RUN_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return None  # 성공
    except FileExistsError:
        # stale 락 체크: PID가 살아있는지 확인
        try:
            pid_str = RUN_LOCK_FILE.read_text(encoding="utf-8").strip()
            pid = int(pid_str)
            os.kill(pid, 0)  # 신호 없이 프로세스 존재만 확인
            return f"claw-log가 이미 실행 중입니다 (PID: {pid}). 잠시 후 다시 시도하세요."
        except (OSError, ValueError):
            # 프로세스가 죽었거나 PID 파싱 실패 → stale 락 제거 후 재시도
            try:
                os.remove(str(RUN_LOCK_FILE))
            except OSError:
                pass
            return acquire_run_lock()


def release_run_lock():
    """PID 락 파일 제거."""
    try:
        os.remove(str(RUN_LOCK_FILE))
    except OSError:
        pass
