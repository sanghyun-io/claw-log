"""
Claw-Log Git Collector
커밋을 개별 단위로 수집하는 모듈. 기존 main.py의 get_git_diff_for_path()를 대체.
"""

import subprocess
import datetime
from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path


# ── 상수 ──

PER_COMMIT_DIFF_LIMIT = 8_000

EXCLUDE_PATTERNS = [
    ":(exclude)package-lock.json", ":(exclude)yarn.lock", ":(exclude)pnpm-lock.yaml",
    ":(exclude)*.map", ":(exclude)dist/", ":(exclude)build/",
    ":(exclude)node_modules/", ":(exclude).next/", ":(exclude).git/", ":(exclude).DS_Store",
]


# ── 데이터클래스 ──

@dataclass
class CommitData:
    hash: str       # Full SHA
    stat: str       # subject + --stat 출력 (항상 전체 포함)
    diff: str       # git show -p (PER_COMMIT_DIFF_LIMIT로 truncate)


@dataclass
class ProjectCommits:
    repo_path: str
    repo_key: str
    project_name: str
    commits: List[CommitData]           # oldest-first
    uncommitted_diff: Optional[str]
    uncommitted_stat: Optional[str]


# ── Git 헬퍼 함수 (main.py에서 이동) ──

def get_repo_key(path):
    """repo_root으로 고유 키 생성 (브랜치 무관 통합 추적)."""
    try:
        repo_root = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stderr=subprocess.STDOUT
        ).decode("utf-8").strip()
    except subprocess.CalledProcessError:
        repo_root = str(Path(path).resolve())
    return repo_root


def is_valid_ancestor(path, commit_hash):
    """커밋이 현재 HEAD의 ancestor인지 확인 (rebase/amend 감지)."""
    try:
        obj_type = subprocess.check_output(
            ["git", "-C", str(path), "cat-file", "-t", commit_hash],
            stderr=subprocess.STDOUT
        ).decode("utf-8").strip()
        if obj_type != "commit":
            return False
        result = subprocess.run(
            ["git", "-C", str(path), "merge-base", "--is-ancestor", commit_hash, "HEAD"],
            capture_output=True
        )
        return result.returncode == 0
    except subprocess.CalledProcessError:
        return False


def get_latest_commit_hash(path):
    """현재 HEAD의 커밋 해시를 반환."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            stderr=subprocess.STDOUT
        ).decode("utf-8").strip()
    except subprocess.CalledProcessError:
        return None


# ── 커밋 수집 함수 ──

def list_commit_hashes(path, last_hash=None, days=0, exclude_hashes=None):
    """지정된 범위의 커밋 해시 목록을 반환 (oldest-first).

    Args:
        path: Git 저장소 경로
        last_hash: 마지막 처리된 커밋 해시 (이후 커밋만 수집)
        days: 과거 N일치 수집 (0이면 오늘만)
        exclude_hashes: 이미 처리된 커밋 해시 set (중복 필터링)

    Returns:
        커밋 해시 리스트 (oldest-first)
    """
    path = Path(path).resolve()
    cmd = ["git", "-C", str(path), "log", "--reverse", "--format=%H"]

    if last_hash and is_valid_ancestor(path, last_hash):
        cmd.append(f"{last_hash}..HEAD")
    elif last_hash:
        # 무효 해시 → fallback: 최근 100개
        p_name = path.name
        print(f"  [{p_name}] 저장된 커밋 해시가 유효하지 않아 최근 커밋을 스캔합니다.")
        cmd.extend(["-n", "100"])
    else:
        # 날짜 기반
        since_date = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if days > 0:
            since_date -= datetime.timedelta(days=days)
        cmd.append(f"--since={since_date.isoformat()}")

    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8").strip()
        if not output:
            return []
        hashes = output.split("\n")
    except subprocess.CalledProcessError:
        return []

    # 이미 처리된 커밋 필터링
    if exclude_hashes:
        hashes = [h for h in hashes if h not in exclude_hashes]

    return hashes


def collect_commit_data(path, commit_hash):
    """단일 커밋의 stat과 diff를 수집.

    Args:
        path: Git 저장소 경로
        commit_hash: 수집할 커밋의 full SHA

    Returns:
        CommitData 인스턴스
    """
    path = Path(path).resolve()

    # stat: subject + file change stats
    try:
        stat = subprocess.check_output(
            ["git", "-C", str(path), "show", "--stat", "--format=%s", commit_hash],
            stderr=subprocess.STDOUT
        ).decode("utf-8", errors="replace").strip()
    except subprocess.CalledProcessError:
        stat = ""

    # diff: patch with exclude patterns, truncated to PER_COMMIT_DIFF_LIMIT
    try:
        cmd = (
            ["git", "-C", str(path), "show", "-p", "--format=", commit_hash, "--", "."]
            + EXCLUDE_PATTERNS
        )
        diff = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8", errors="replace")
        if len(diff) > PER_COMMIT_DIFF_LIMIT:
            diff = diff[:PER_COMMIT_DIFF_LIMIT] + \
                f"\n\n... (diff truncated at {PER_COMMIT_DIFF_LIMIT:,} chars) ..."
    except subprocess.CalledProcessError:
        diff = ""

    return CommitData(hash=commit_hash, stat=stat, diff=diff)


def collect_uncommitted(path):
    """미커밋 변경사항의 stat과 diff를 수집.

    Args:
        path: Git 저장소 경로

    Returns:
        (stat, diff) 튜플. 변경사항 없으면 (None, None).
    """
    path = Path(path).resolve()

    try:
        cmd_stat = ["git", "-C", str(path), "diff", "HEAD", "--stat", "--", "."] + EXCLUDE_PATTERNS
        stat = subprocess.check_output(cmd_stat, stderr=subprocess.STDOUT).decode("utf-8", errors="replace").strip()
    except subprocess.CalledProcessError:
        stat = None

    try:
        cmd_diff = ["git", "-C", str(path), "diff", "HEAD", "--", "."] + EXCLUDE_PATTERNS
        diff = subprocess.check_output(cmd_diff, stderr=subprocess.STDOUT).decode("utf-8", errors="replace")
        if len(diff) > PER_COMMIT_DIFF_LIMIT:
            diff = diff[:PER_COMMIT_DIFF_LIMIT] + \
                f"\n\n... (uncommitted diff truncated at {PER_COMMIT_DIFF_LIMIT:,} chars) ..."
    except subprocess.CalledProcessError:
        diff = None

    if not stat and not diff:
        return (None, None)

    return (stat or None, diff or None)


def collect_project_commits(repo_path, repo_key, last_hash=None, days=0, exclude_hashes=None):
    """프로젝트의 모든 커밋 데이터를 수집.

    Args:
        repo_path: Git 저장소 경로
        repo_key: 프로젝트 고유 키 (상태 추적용)
        last_hash: 마지막 처리된 커밋 해시
        days: 과거 N일치 수집 (0이면 오늘만)
        exclude_hashes: 이미 처리된 커밋 해시 set (중복 필터링)

    Returns:
        ProjectCommits 인스턴스
    """
    path = Path(repo_path).resolve()
    project_name = path.name

    empty = ProjectCommits(
        repo_path=str(path), repo_key=repo_key,
        project_name=project_name, commits=[],
        uncommitted_diff=None, uncommitted_stat=None,
    )

    if not path.exists():
        print(f"   경로를 찾을 수 없습니다: {path}")
        return empty

    if not (path / ".git").exists():
        print(f"   Git 저장소가 아닙니다 (건너뜀): {path}")
        return empty

    # 1. 커밋 해시 목록 수집
    hashes = list_commit_hashes(path, last_hash=last_hash, days=days, exclude_hashes=exclude_hashes)

    # 2. 각 커밋 데이터 수집 (stat이 비어있으면 스킵 — merge 커밋 등)
    commits = []
    for h in hashes:
        cd = collect_commit_data(path, h)
        if cd.stat:
            commits.append(cd)

    # 3. 미커밋 변경사항 (days==0일 때만)
    uncommitted_stat = None
    uncommitted_diff = None
    if days == 0:
        uncommitted_stat, uncommitted_diff = collect_uncommitted(path)

    return ProjectCommits(
        repo_path=str(path), repo_key=repo_key,
        project_name=project_name, commits=commits,
        uncommitted_diff=uncommitted_diff, uncommitted_stat=uncommitted_stat,
    )
