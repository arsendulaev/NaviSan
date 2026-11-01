"""Управление самообновлением NaviSan через Git и автоматический перезапуск."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent

# Папка постоянного хранения маркеров обновления
_STAGING_DIR = Path(os.environ.get("NAVISAN_STAGING", "/staging"))


def _get_staging_dir() -> Path:
    if _STAGING_DIR.exists() and _STAGING_DIR.is_dir():
        return _STAGING_DIR
    local_staging = REPO_ROOT / "staging"
    if local_staging.exists() and local_staging.is_dir():
        return local_staging
    return REPO_ROOT


def _marker_path() -> Path:
    return _get_staging_dir() / ".update_marker.json"


def _run_git(args: list[str], timeout: int = 45) -> tuple[int, str, str]:
    """Выполняет команду git в корне репозитория с безопасными настройками URL и прав."""
    git_bin = shutil.which("git")
    if not git_bin:
        return 1, "", "Команда git не найдена"

    cmd = [
        git_bin,
        "-C", str(REPO_ROOT),
        "-c", "safe.directory=*",
        "-c", 'url.https://github.com/.insteadOf=git@github.com:',
    ] + args

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Превышено время ожидания git команды ({timeout} сек)"
    except Exception as e:
        return 1, "", str(e)


def get_current_commit() -> dict[str, str]:
    """Возвращает информацию о текущем коммите репозитория."""
    rc, sha, _ = _run_git(["rev-parse", "HEAD"])
    if rc != 0 or not sha:
        return {"sha": "unknown", "short": "unknown", "message": "", "date": ""}

    _, short, _ = _run_git(["rev-parse", "--short", "HEAD"])
    _, msg, _ = _run_git(["log", "-1", "--pretty=%s"])
    _, date, _ = _run_git(["log", "-1", "--pretty=%cd", "--date=format:%Y-%m-%d %H:%M"])

    return {
        "sha": sha,
        "short": short or sha[:7],
        "message": msg or "",
        "date": date or "",
    }


def check_for_updates() -> dict[str, Any]:
    """Проверяет наличие новых коммитов на GitHub (origin/main).

    Returns:
        dict со статусом:
          - "up_to_date": обновление не требуется
          - "available": есть новые коммиты
          - "error": ошибка проверки (нет git / нет сети / нет репозитория)
    """
    if not shutil.which("git"):
        return {
            "status": "error",
            "error": "Утилита git не установлена в контейнере",
        }

    git_dir = REPO_ROOT / ".git"
    if not git_dir.exists():
        return {
            "status": "error",
            "error": "Папка репозитория .git не примонтирована в контейнер",
        }

    current = get_current_commit()

    # 1. Получаем SHA удалённого репозитория через ls-remote (не требует записи на диск!)
    rc, ls_out, err = _run_git(["ls-remote", "origin", "HEAD"])
    remote_sha = ""
    if rc == 0 and ls_out:
        remote_sha = ls_out.split()[0].strip()

    if not remote_sha:
        # Фоллбэк через git fetch
        rc_f, _, err_f = _run_git(["fetch", "origin", "main"])
        if rc_f == 0:
            _, remote_sha, _ = _run_git(["rev-parse", "origin/main"])
        else:
            return {
                "status": "error",
                "error": f"Не удалось связаться с GitHub: {err or err_f}",
                "current": current,
            }

    if current["sha"] == remote_sha:
        return {
            "status": "up_to_date",
            "current": current,
        }

    # Есть новые коммиты
    commits: list[str] = []
    reqs_changed = False
    target = {
        "sha": remote_sha,
        "short": remote_sha[:7],
        "message": "",
        "date": "",
    }

    # Пытаемся получить список коммитов локально через fetch
    rc_f, _, _ = _run_git(["fetch", "origin", "main"])
    if rc_f == 0:
        rc, log_out, _ = _run_git(["log", "HEAD..origin/main", "--oneline", "-n", "10"])
        commits = [line.strip() for line in log_out.splitlines() if line.strip()]
        rc, diff_files, _ = _run_git(["diff", "--name-only", "HEAD", "origin/main"])
        changed_files = [f.strip() for f in diff_files.splitlines() if f.strip()]
        reqs_changed = "requirements.txt" in changed_files

        _, remote_short, _ = _run_git(["rev-parse", "--short", "origin/main"])
        _, remote_msg, _ = _run_git(["log", "-1", "--pretty=%s", "origin/main"])
        _, remote_date, _ = _run_git(["log", "-1", "--pretty=%cd", "--date=format:%Y-%m-%d %H:%M", "origin/main"])
        target["short"] = remote_short or target["short"]
        target["message"] = remote_msg or ""
        target["date"] = remote_date or ""
    else:
        # Fallback через публичный GitHub API (не требует записи в .git)
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://api.github.com/repos/arsendulaev/NaviSan/commits?sha=main&per_page=10",
                headers={"User-Agent": "NaviSan"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                for item in data:
                    c_sha = item.get("sha", "")
                    if c_sha == current["sha"]:
                        break
                    c_msg = item.get("commit", {}).get("message", "").splitlines()[0]
                    commits.append(f"{c_sha[:7]} {c_msg}")
                if data:
                    first = data[0]
                    target["message"] = first.get("commit", {}).get("message", "").splitlines()[0]
                    d_raw = first.get("commit", {}).get("author", {}).get("date", "")
                    target["date"] = d_raw[:16].replace("T", " ") if d_raw else ""
        except Exception as e:
            log.warning("Не удалось получить коммиты через GitHub API: %s", e)

    return {
        "status": "available",
        "current": current,
        "target": target,
        "commits": commits,
        "count": len(commits),
        "reqs_changed": reqs_changed,
    }


def perform_update(chat_id: int | None = None, message_id: int | None = None) -> dict[str, Any]:
    """Выполняет git pull origin main и сохраняет маркер обновления.

    Returns:
        {"success": bool, "error": str | None, "new_commit": dict, "reqs_changed": bool}
    """
    old_commit = get_current_commit()

    # Стягиваем изменения
    rc, stdout, stderr = _run_git(["pull", "origin", "main"])
    if rc != 0:
        err = stderr or stdout or "Неизвестная ошибка git pull"
        if "Permission denied" in err or "FETCH_HEAD" in err:
            err = (
                "Ошибка прав доступа к файлам на сервере (Permission denied).\n"
                "Выполните на сервере в папке NaviSan:\n"
                "sudo chown -R 1000:1000 ."
            )
        log.error("Сбой git pull origin main: %s", err)
        return {
            "success": False,
            "error": err,
            "new_commit": old_commit,
            "reqs_changed": False,
        }

    new_commit = get_current_commit()

    # Проверяем, изменился ли requirements.txt
    rc, diff_files, _ = _run_git(["diff", "--name-only", old_commit["sha"], new_commit["sha"]])
    changed_files = [f.strip() for f in diff_files.splitlines() if f.strip()]
    reqs_changed = "requirements.txt" in changed_files

    # Сохраняем маркер для приветственного сообщения после перезапуска
    marker_data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "old_commit": old_commit,
        "new_commit": new_commit,
        "reqs_changed": reqs_changed,
        "timestamp": time.time(),
    }
    try:
        mp = _marker_path()
        mp.write_text(json.dumps(marker_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Не удалось сохранить маркер обновления: %s", e)

    log.info(
        "Обновление успешно установлено (%s -> %s). Reqs changed: %s",
        old_commit["short"], new_commit["short"], reqs_changed
    )

    return {
        "success": True,
        "error": None,
        "new_commit": new_commit,
        "reqs_changed": reqs_changed,
    }


def get_and_clear_update_marker() -> dict[str, Any] | None:
    """Считывает и удаляет маркер завершённого обновления (при старте нового процесса)."""
    mp = _marker_path()
    if not mp.exists():
        return None

    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
        mp.unlink(missing_ok=True)
        return data
    except Exception as e:
        log.warning("Ошибка чтения маркера обновления: %s", e)
        try:
            mp.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def restart_process(delay: float = 1.0) -> None:
    """Завершает текущий процесс через delay секунд для перезапуска Docker-контейнером."""
    def _shutdown() -> None:
        time.sleep(delay)
        log.info("Завершение процесса для перезапуска контейнера (exit code 0)")
        # os._exit(0) гарантированно завершает процесс без зависания на потоках
        os._exit(0)

    t = threading.Thread(target=_shutdown, daemon=True, name="NavisanRestartThread")
    t.start()
