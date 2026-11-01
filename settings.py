"""Управление настройками NaviSan с персистентным сохранением на диск."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Режимы ReplayGain
RG_MODE_WRITE = "write"
RG_MODE_STRIP = "strip"
RG_MODE_SKIP = "skip"

RG_MODE_DESCRIPTIONS: dict[str, dict[str, str]] = {
    RG_MODE_WRITE: {
        "title": "🟢 Рассчитывать (по умолчанию)",
        "short": "🟢 Расчёт",
        "desc": "metaflac рассчитывает Track & Album Gain с автолечением потоков и сохраняет теги.",
    },
    RG_MODE_STRIP: {
        "title": "✂️ Вырезать как мусор",
        "short": "✂️ Вырезать",
        "desc": "Теги ReplayGain удаляются при любой обработке как лишний мусор, расчёт отключён.",
    },
    RG_MODE_SKIP: {
        "title": "⚪ Пропускать (не трогать)",
        "short": "⚪ Пропускать",
        "desc": "Расчёт ReplayGain не производится, существующие теги файлов остаются нетронутыми.",
    },
}

def _get_settings_file() -> Path:
    custom_path = os.environ.get("NAVISAN_SETTINGS_PATH")
    if custom_path:
        return Path(custom_path)
    staging_dir = Path(os.environ.get("NAVISAN_STAGING", "/staging"))
    if staging_dir.exists() and staging_dir.is_dir():
        return staging_dir / ".navisan_settings.json"
    local_staging = Path("staging")
    if local_staging.exists() and local_staging.is_dir():
        return local_staging / ".navisan_settings.json"
    return Path(__file__).parent / "settings.json"


_cache: dict[str, Any] | None = None


def load_settings() -> dict[str, Any]:
    """Загружает настройки из JSON-файла с кэшированием в памяти."""
    global _cache
    if _cache is not None:
        return _cache

    f = _get_settings_file()
    data: dict[str, Any] = {}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Не удалось прочитать файл настроек %s: %s", f, e)

    _cache = data
    return _cache


def save_settings(data: dict[str, Any]) -> bool:
    """Атомарно сохраняет настройки на диск."""
    global _cache
    _cache = data
    f = _get_settings_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.parent / f".tmp_{f.name}"
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
        log.info("Настройки успешно сохранены в %s", f)
        return True
    except Exception as e:
        log.error("Ошибка сохранения настроек в %s: %s", f, e)
        return False


def get_replaygain_mode() -> str:
    """Возвращает режим работы ReplayGain: 'write', 'strip' или 'skip'."""
    settings = load_settings()
    mode = settings.get("replaygain_mode")
    if mode in (RG_MODE_WRITE, RG_MODE_STRIP, RG_MODE_SKIP):
        return mode

    env_mode = os.environ.get("NAVISAN_REPLAYGAIN_MODE", RG_MODE_WRITE).strip().lower()
    if env_mode in (RG_MODE_WRITE, RG_MODE_STRIP, RG_MODE_SKIP):
        return env_mode

    return RG_MODE_WRITE


def set_replaygain_mode(mode: str) -> bool:
    """Устанавливает и сохраняет режим ReplayGain: 'write', 'strip' или 'skip'."""
    mode = mode.strip().lower()
    if mode not in (RG_MODE_WRITE, RG_MODE_STRIP, RG_MODE_SKIP):
        raise ValueError(
            f"Недопустимый режим ReplayGain: '{mode}'. "
            f"Допустимо: '{RG_MODE_WRITE}', '{RG_MODE_STRIP}', '{RG_MODE_SKIP}'"
        )

    settings = dict(load_settings())
    settings["replaygain_mode"] = mode
    return save_settings(settings)
