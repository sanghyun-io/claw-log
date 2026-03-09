import os
import sys
import subprocess
import platform
from pathlib import Path

SCHEDULER_LOG = "scheduler.log"
CRON_COMMENT = "# ClawLog Daily Schedule"
WIN_TASK_NAME = "ClawLog_Daily"


def _is_wsl():
    """WSL 환경인지 감지 (결과 캐싱)."""
    if hasattr(_is_wsl, '_cached'):
        return _is_wsl._cached
    if os.environ.get('WSL_DISTRO_NAME'):
        _is_wsl._cached = True
        return True
    try:
        with open('/proc/version', 'r') as f:
            result = 'microsoft' in f.read().lower()
            _is_wsl._cached = result
            return result
    except (FileNotFoundError, PermissionError):
        _is_wsl._cached = False
        return False


def _get_wsl_distro_name():
    """WSL 배포판 이름 반환. 없으면 None."""
    return os.environ.get('WSL_DISTRO_NAME')


def _get_platform():
    """'windows', 'wsl', 'unix' 중 하나 반환."""
    if platform.system() == "Windows":
        return "windows"
    if _is_wsl():
        return "wsl"
    return "unix"


def _get_cron_info():
    """현재 crontab에서 ClawLog 스케줄 정보를 읽어옵니다."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode != 0:
            return None, []

        lines = result.stdout.splitlines()
        claw_lines = []
        for line in lines:
            if "claw_log.main" in line and not line.strip().startswith("#"):
                claw_lines.append(line)

        return result.stdout, claw_lines
    except Exception:
        return None, []


def _get_win_schedule_info(schtasks_cmd="schtasks"):
    """Windows 작업 스케줄러에서 ClawLog 정보를 읽어옵니다."""
    try:
        result = subprocess.run(
            [schtasks_cmd, "/Query", "/TN", WIN_TASK_NAME, "/FO", "LIST", "/V"],
            capture_output=True
        )
        if result.returncode != 0:
            return None
        # WSL에서 schtasks.exe 출력은 Windows ANSI 인코딩(cp949)일 수 있음
        for enc in ('utf-8', 'cp949', 'euc-kr'):
            try:
                return result.stdout.decode(enc)
            except UnicodeDecodeError:
                continue
        return result.stdout.decode('utf-8', errors='replace')
    except Exception:
        return None


def get_schedule_summary():
    """스케줄 정보를 문자열로 반환합니다 (--status용)."""
    pf = _get_platform()
    schtasks_cmd = "schtasks.exe" if pf == "wsl" else "schtasks"

    if pf in ("windows", "wsl"):
        info = _get_win_schedule_info(schtasks_cmd)
        if info:
            for line in info.splitlines():
                line = line.strip()
                if any(k in line for k in ["시작 시간", "Start Time"]):
                    time_val = line.split(":", 1)[-1].strip() if ":" in line else ""
                    # "시작 시간:       오전 11:30:00" 등에서 시각 추출
                    return f"매일 {time_val}" if time_val else "등록됨"
            return "등록됨"
        return "⚠️ 미등록"
    else:
        _, claw_lines = _get_cron_info()
        if claw_lines:
            parts = claw_lines[0].split()
            if len(parts) >= 2:
                minute, hour = parts[0], parts[1]
                return f"매일 {hour.zfill(2)}:{minute.zfill(2)}"
            return "등록됨"
        return "⚠️ 미등록"


def show_schedule():
    """현재 등록된 스케줄 정보를 출력합니다."""
    pf = _get_platform()
    schtasks_cmd = "schtasks.exe" if pf == "wsl" else "schtasks"

    print("\n📋 현재 Claw-Log 스케줄 정보")
    print("=" * 50)

    if pf in ("windows", "wsl"):
        info = _get_win_schedule_info(schtasks_cmd)
        if info:
            for line in info.splitlines():
                line = line.strip()
                if any(k in line for k in ["작업 이름", "Task Name", "다음 실행", "Next Run",
                                           "상태", "Status", "예약 유형", "Schedule Type",
                                           "시작 시간", "Start Time", "Start Date"]):
                    print(f"   {line}")
            print("=" * 50)
        else:
            print("   ⚠️ 등록된 스케줄이 없습니다.")
            print("   👉 'claw-log --schedule 23:30' 으로 등록하세요.")
    else:
        _, claw_lines = _get_cron_info()
        if claw_lines:
            for cron_line in claw_lines:
                parts = cron_line.split()
                if len(parts) >= 5:
                    minute, hour = parts[0], parts[1]
                    cmd_part = " ".join(parts[5:])
                    cwd = ""
                    if "cd " in cmd_part:
                        cwd = cmd_part.split("cd ")[1].split(" &&")[0]

                    print(f"   ⏰ 실행 시각: 매일 {hour.zfill(2)}:{minute.zfill(2)}")
                    if cwd:
                        print(f"   📂 실행 경로: {cwd}")
                    print(f"   📝 Cron 원문: {cron_line}")
            print("=" * 50)
        else:
            print("   ⚠️ 등록된 스케줄이 없습니다.")
            print("   👉 'claw-log --schedule 23:30' 으로 등록하세요.")


def remove_schedule():
    """등록된 스케줄을 삭제합니다."""
    pf = _get_platform()
    schtasks_cmd = "schtasks.exe" if pf == "wsl" else "schtasks"

    if pf in ("windows", "wsl"):
        try:
            subprocess.run(
                [schtasks_cmd, "/Delete", "/TN", WIN_TASK_NAME, "/F"],
                check=True
            )
            print("✅ Windows 스케줄 삭제 완료!")
        except subprocess.CalledProcessError:
            print("⚠️ 삭제할 스케줄이 없거나 실패했습니다.")
        except FileNotFoundError:
            print("❌ schtasks.exe를 찾을 수 없습니다. WSL interop 비활성화 가능성이 있습니다.")
    else:
        full_cron, claw_lines = _get_cron_info()
        if not claw_lines:
            print("⚠️ 삭제할 스케줄이 없습니다.")
            return

        try:
            lines = full_cron.splitlines()
            new_lines = [
                line for line in lines
                if "claw_log.main" not in line and line.strip() != CRON_COMMENT
            ]
            new_cron = "\n".join(new_lines) + "\n"

            process = subprocess.Popen(
                ["crontab", "-"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            _, stderr = process.communicate(input=new_cron)

            if process.returncode == 0:
                print("✅ Crontab 스케줄 삭제 완료!")
            else:
                print(f"❌ Crontab 삭제 실패: {stderr}")
        except Exception as e:
            print(f"❌ Crontab 접근 실패: {e}")


def install_schedule(schedule_time="23:30"):
    """
    OS별 스케줄러 등록 (사용자 지정 시각에 실행)
    """
    pf = _get_platform()
    python_executable = sys.executable

    hour, minute = schedule_time.split(":")
    hour = hour.zfill(2)
    minute = minute.zfill(2)

    cwd = os.getcwd()
    log_file_path = os.path.join(cwd, SCHEDULER_LOG)

    print(f"\n🕒 [{pf}] 스케줄러 등록 작업 시작...")
    print(f"   - 실행 시각: 매일 {hour}:{minute}")
    print(f"   - 실행 경로: {cwd}")
    print(f"   - 로그 파일: {log_file_path}")

    if pf == "windows":
        cmd_str = f'cd /d "{cwd}" && set PYTHONIOENCODING=utf-8 && "{python_executable}" -m claw_log.main >> "{log_file_path}" 2>&1'
        win_cmd = f'cmd /c "{cmd_str}"'
        try:
            subprocess.run([
                "schtasks", "/Create", "/SC", "DAILY", "/TN", WIN_TASK_NAME,
                "/TR", win_cmd, "/ST", f"{hour}:{minute}", "/F"
            ], check=True)
            print(f"✅ Windows 작업 스케줄러에 '{WIN_TASK_NAME}' 등록 완료!")
        except subprocess.CalledProcessError as e:
            print(f"❌ Windows 스케줄러 등록 실패: {e}")
        except FileNotFoundError:
            print("❌ schtasks를 찾을 수 없습니다.")
    elif pf == "wsl":
        bash_cmd = f"cd '{cwd}' && '{python_executable}' -m claw_log.main >> '{log_file_path}' 2>&1"
        distro = _get_wsl_distro_name()
        if distro:
            wsl_cmd = f'wsl.exe -d {distro} -- bash -c "{bash_cmd}"'
        else:
            wsl_cmd = f'wsl.exe -- bash -c "{bash_cmd}"'
        try:
            subprocess.run([
                "schtasks.exe", "/Create", "/SC", "DAILY", "/TN", WIN_TASK_NAME,
                "/TR", wsl_cmd, "/ST", f"{hour}:{minute}", "/F"
            ], check=True)
            print(f"✅ Windows 작업 스케줄러에 '{WIN_TASK_NAME}' 등록 완료! (WSL via wsl.exe)")
        except subprocess.CalledProcessError as e:
            print(f"❌ Windows 스케줄러 등록 실패: {e}")
        except FileNotFoundError:
            print("❌ schtasks.exe를 찾을 수 없습니다. WSL interop 비활성화 가능성이 있습니다.")
    else:
        cmd_str = f"cd '{cwd}' && '{python_executable}' -m claw_log.main >> '{log_file_path}' 2>&1"
        cron_job = f"{minute} {hour} * * * {cmd_str}"
        try:
            current_cron = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True
            ).stdout

            lines = current_cron.splitlines()
            new_lines = [
                line for line in lines
                if "claw_log.main" not in line and line.strip() != CRON_COMMENT
            ]
            new_cron = "\n".join(new_lines) + f"\n{CRON_COMMENT}\n{cron_job}\n"

            process = subprocess.Popen(
                ["crontab", "-"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            _, stderr = process.communicate(input=new_cron)

            if process.returncode == 0:
                print(f"✅ Crontab에 스케줄 등록/업데이트 완료! (매일 {hour}:{minute})")
            else:
                print(f"❌ Crontab 등록 실패: {stderr}")
        except Exception as e:
            print(f"❌ Crontab 접근 실패: {e}")
