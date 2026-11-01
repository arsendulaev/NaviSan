"""Модуль расчёта и управления ReplayGain для FLAC-библиотеки.

Использует официальную утилиту metaflac (Xiph.Org) для быстрого неразрушающего
расчёта психоакустической громкости (EBU R128 / ReplayGain 2.0).
Записывает в теги каждого трека:
- REPLAYGAIN_TRACK_GAIN / REPLAYGAIN_TRACK_PEAK
- REPLAYGAIN_ALBUM_GAIN / REPLAYGAIN_ALBUM_PEAK
- REPLAYGAIN_REFERENCE_LOUDNESS
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from mutagen.flac import FLAC

log = logging.getLogger(__name__)


def find_metaflac() -> str | None:
    """Ищет исполняемый файл metaflac в системе."""
    # 1. Системный PATH
    found = shutil.which("metaflac")
    if found:
        return found

    # 2. Пользовательский ~/.local/bin
    user_bin = Path.home() / ".local/bin/metaflac"
    if user_bin.exists() and os.access(user_bin, os.X_OK):
        return str(user_bin)

    # 3. Стандартные пути Linux
    for p in ("/usr/bin/metaflac", "/usr/local/bin/metaflac", "/bin/metaflac"):
        if Path(p).exists():
            return p

    return None


def find_flac() -> str | None:
    """Ищет исполняемый файл flac в системе."""
    # 1. Системный PATH
    found = shutil.which("flac")
    if found:
        return found

    # 2. Пользовательский ~/.local/bin
    user_bin = Path.home() / ".local/bin/flac"
    if user_bin.exists() and os.access(user_bin, os.X_OK):
        return str(user_bin)

    # 3. Стандартные пути Linux
    for p in ("/usr/bin/flac", "/usr/local/bin/flac", "/bin/flac"):
        if Path(p).exists():
            return p

    return None


def repair_flac_stream(track_path: str | Path) -> bool:
    """Восстанавливает повреждённый FLAC-аудиопоток (flac -d -F | flac) с сохранением тегов.

    Строгий декодер metaflac падает при малейшей рассинхронизации фреймов (LOST_SYNC).
    Данная функция неразрушающе пересобирает аудиопоток через встроенный декодер
    FLAC с ключом -F (decode through errors), сохраняя оригинальные теги и обложки.
    """
    flac_bin = find_flac()
    if not flac_bin:
        return False

    src = Path(track_path)
    if not src.exists():
        return False

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_out = Path(tmp_dir) / f"repaired_{src.name}"
        try:
            p1 = subprocess.Popen(
                [flac_bin, "-d", "-F", "-c", str(src)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            p2 = subprocess.Popen(
                [flac_bin, "-s", "-", "-o", str(tmp_out)],
                stdin=p1.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if p1.stdout:
                p1.stdout.close()
            _, err = p2.communicate(timeout=60)
            if p2.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
                log.warning("Не удалось восстановить поток для %s: %s", src.name, err.decode(errors="replace").strip())
                return False

            # Переносим все теги и обложки
            src_audio = FLAC(str(src))
            dst_audio = FLAC(str(tmp_out))
            for k, v in src_audio.items():
                dst_audio[k] = v
            for pic in src_audio.pictures:
                dst_audio.add_picture(pic)
            dst_audio.save()

            shutil.move(str(tmp_out), str(src))
            log.info("✓ Аудиопоток успешно восстановлен: %s", src.name)
            return True
        except Exception as e:
            log.warning("Ошибка при восстановлении потока %s: %s", src.name, e)
            return False


def has_replaygain(track_path: str | Path) -> bool:
    """Проверяет наличие ReplayGain тегов в FLAC-файле."""
    try:
        audio = FLAC(str(track_path))
        gain = audio.get("replaygain_track_gain")
        return bool(gain and gain[0].strip())
    except Exception:
        return False


def album_has_replaygain(album_dir: str | Path) -> bool:
    """Проверяет, проставлен ли ReplayGain для всех треков альбома."""
    root = Path(album_dir)
    flacs = sorted(root.rglob("*.flac"))
    if not flacs:
        return False
    return all(has_replaygain(f) for f in flacs)


def apply_replaygain(album_dir: str | Path) -> tuple[bool, str]:
    """Рассчитывает и записывает ReplayGain (потрековый и альбомный) для всех FLAC в папке.

    Запускает metaflac --add-replay-gain на все треки альбома разом,
    чтобы рассчитать как индивидуальный Track Gain каждого трека,
    так и единый Album Gain для всего релиза.
    Если треки имеют разные аудиоформаты (sample rate / bit depth) или среди них есть
    повреждённый файл, выполняет адаптивный расчёт по совместимым группам и потреково.

    Returns:
        (успех: bool, сообщение: str)
    """
    root = Path(album_dir)
    flacs = sorted(root.rglob("*.flac"))
    if not flacs:
        return False, "В папке нет FLAC-файлов"

    metaflac_bin = find_metaflac()
    if not metaflac_bin:
        err = "Утилита metaflac не найдена в системе"
        log.warning(err)
        return False, err

    # 1. Попытка единого альбомного расчёта для всех треков разом
    cmd = [metaflac_bin, "--add-replay-gain"] + [str(f) for f in flacs]
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            log.info("✓ ReplayGain успешно рассчитан для альбома %s (%d треков)", root.name, len(flacs))
            return True, f"ReplayGain рассчитан для {len(flacs)} треков"

        batch_err = proc.stderr.strip() or f"Код ошибки: {proc.returncode}"
        last_err = batch_err.splitlines()[-1] if batch_err else "несоответствие форматов"
        log.info(
            "Альбомный расчёт ReplayGain для %s требует адаптивной обработки (%s)",
            root.name, last_err
        )

        # Если сбой вызван ошибкой декодирования битых фреймов — запускаем лечение потоков
        if "during analysis" in batch_err or "LOST_SYNC" in batch_err:
            log.info("Обнаружены ошибки декодирования в альбоме %s, запускаем автолечение потоков...", root.name)
            repaired_any = False
            for f in flacs:
                chk = subprocess.run([metaflac_bin, "--add-replay-gain", str(f)], capture_output=True, text=True, timeout=30)
                if chk.returncode != 0:
                    if repair_flac_stream(f):
                        repaired_any = True

            if repaired_any:
                retry_proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if retry_proc.returncode == 0:
                    log.info("✓ ReplayGain успешно рассчитан для альбома %s после лечения потоков (%d треков)", root.name, len(flacs))
                    return True, f"ReplayGain рассчитан для {len(flacs)} треков (потоки восстановлены)"
    except Exception as e:
        log.warning("Сбой пакетного запуска metaflac для %s: %s", root.name, e)

    # 2. Адаптивный фоллбэк: группировка треков по аудиоформату (каналы, частота, разрядность)
    groups: dict[tuple[int, int, int], list[Path]] = {}
    for f in flacs:
        try:
            audio = FLAC(str(f))
            key = (audio.info.channels, audio.info.sample_rate, audio.info.bits_per_sample)
        except Exception:
            key = (2, 44100, 16)
        groups.setdefault(key, []).append(f)

    success_count = 0
    failed_files: list[str] = []

    for fmt_key, group_flacs in groups.items():
        if len(group_flacs) > 1:
            group_cmd = [metaflac_bin, "--add-replay-gain"] + [str(f) for f in group_flacs]
            group_proc = subprocess.run(group_cmd, capture_output=True, text=True, timeout=120)
            if group_proc.returncode == 0:
                success_count += len(group_flacs)
                continue

        # Потрековый расчёт для файлов группы (или если в группе есть повреждённый файл)
        for f in group_flacs:
            single_cmd = [metaflac_bin, "--add-replay-gain", str(f)]
            single_proc = subprocess.run(single_cmd, capture_output=True, text=True, timeout=30)
            if single_proc.returncode == 0:
                success_count += 1
            else:
                # Пробуем вылечить повреждённый поток и повторить расчёт
                if repair_flac_stream(f):
                    retry_single = subprocess.run(single_cmd, capture_output=True, text=True, timeout=30)
                    if retry_single.returncode == 0:
                        success_count += 1
                        continue

                err_msg = single_proc.stderr.strip().splitlines()[-1] if single_proc.stderr else "ошибка декодирования"
                log.warning("Пропуск ReplayGain для %s: %s", f.name, err_msg)
                failed_files.append(f.name)

    if success_count > 0:
        if failed_files:
            log.info(
                "✓ ReplayGain рассчитан для %d из %d треков альбома %s (пропущено повреждённых: %d)",
                success_count, len(flacs), root.name, len(failed_files)
            )
            return True, f"ReplayGain рассчитан для {success_count}/{len(flacs)} треков (пропущено: {len(failed_files)})"
        else:
            log.info(
                "✓ ReplayGain успешно рассчитан (адаптивно по форматам) для всех %d треков альбома %s",
                success_count, root.name
            )
            return True, f"ReplayGain рассчитан для {success_count} треков"

    err = f"Не удалось рассчитать ReplayGain для {root.name}"
    log.error(err)
    return False, err


def remove_replaygain(album_dir: str | Path) -> tuple[bool, str]:
    """Удаляет ReplayGain теги из всех FLAC в папке."""
    root = Path(album_dir)
    flacs = sorted(root.rglob("*.flac"))
    if not flacs:
        return False, "В папке нет FLAC-файлов"

    metaflac_bin = find_metaflac()
    if not metaflac_bin:
        return False, "Утилита metaflac не найдена"

    cmd = [metaflac_bin, "--remove-replay-gain"] + [str(f) for f in flacs]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return proc.returncode == 0, "ReplayGain удалён" if proc.returncode == 0 else proc.stderr
    except Exception as e:
        return False, str(e)


def audit_library_replaygain(library_dir: str | Path) -> dict:
    """Аудит наличия ReplayGain по всей библиотеке."""
    root = Path(library_dir)
    if not root.exists():
        return {"total": 0, "with_gain": 0, "missing": 0, "albums_missing": []}

    all_albums = sorted([
        d for d in root.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.rglob("*.flac") if f.is_file())
    ])

    total_tracks = 0
    with_gain_tracks = 0
    missing_gain_tracks = 0
    albums_missing = []

    for alb in all_albums:
        flacs = sorted(alb.rglob("*.flac"))
        if not flacs:
            continue
        total_tracks += len(flacs)

        alb_has_missing = False
        for f in flacs:
            if has_replaygain(f):
                with_gain_tracks += 1
            else:
                missing_gain_tracks += 1
                alb_has_missing = True

        if alb_has_missing:
            albums_missing.append(alb)

    return {
        "total": total_tracks,
        "with_gain": with_gain_tracks,
        "missing": missing_gain_tracks,
        "albums_missing": albums_missing,
    }


def strip_library_replaygain(
    library_dir: str | Path,
    progress_callback: Any | None = None,
    cancel_flag: Any | None = None,
) -> dict[str, int]:
    """Удаляет ReplayGain теги со всех FLAC-треков в библиотеке.

    Args:
        library_dir: Путь к корню библиотеки.
        progress_callback: Опциональный callback(current, total, album_name).
        cancel_flag: Опциональная функция () -> bool для досрочной отмены.

    Returns:
        dict: {"total_tracks": int, "stripped_tracks": int, "albums_affected": int}
    """
    root = Path(library_dir)
    if not root.exists():
        return {"total_tracks": 0, "stripped_tracks": 0, "albums_affected": 0}

    all_albums = sorted([
        d for d in root.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.rglob("*.flac") if f.is_file())
    ])

    metaflac_bin = find_metaflac()
    rg_keys = (
        "replaygain_track_gain",
        "replaygain_track_peak",
        "replaygain_album_gain",
        "replaygain_album_peak",
        "replaygain_reference_loudness",
    )

    total_tracks = 0
    stripped_tracks = 0
    albums_affected = 0
    tot_albums = len(all_albums)

    for i, alb in enumerate(all_albums, 1):
        if cancel_flag and cancel_flag():
            break

        flacs = sorted(alb.rglob("*.flac"))
        if not flacs:
            continue
        total_tracks += len(flacs)

        has_rg_list = [f for f in flacs if has_replaygain(f)]
        if not has_rg_list:
            if progress_callback:
                progress_callback(i, tot_albums, alb.name)
            continue

        stripped_here = False
        if metaflac_bin:
            cmd = [metaflac_bin, "--remove-replay-gain"] + [str(f) for f in has_rg_list]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if proc.returncode == 0:
                    stripped_tracks += len(has_rg_list)
                    albums_affected += 1
                    stripped_here = True
            except Exception as e:
                log.warning("Ошибка metaflac при удалении RG в %s: %s", alb.name, e)

        if not stripped_here:
            # Fallback через mutagen
            alb_mod = False
            for f in has_rg_list:
                try:
                    audio = FLAC(str(f))
                    modified = False
                    for k in list(audio.keys()):
                        if k.lower() in rg_keys:
                            del audio[k]
                            modified = True
                    if modified:
                        audio.save()
                        stripped_tracks += 1
                        alb_mod = True
                except Exception as e:
                    log.warning("Ошибка mutagen при удалении RG из %s: %s", f.name, e)
            if alb_mod:
                albums_affected += 1

        if progress_callback:
            progress_callback(i, tot_albums, alb.name)

    log.info(
        "Очистка ReplayGain завершена: затронуто %d альбомов, очищено %d/%d треков",
        albums_affected, stripped_tracks, total_tracks
    )
    return {
        "total_tracks": total_tracks,
        "stripped_tracks": stripped_tracks,
        "albums_affected": albums_affected,
    }

