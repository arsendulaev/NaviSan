"""Staging pipeline: Inbox → Staging → обработка → Library (ADR-0008, ADR-0011).

Модуль отвечает за безопасный перенос релиза через промежуточный каталог
Staging, применение всех шагов обработки (теги, обложка, тексты,
переименование) и атомарный перенос готового результата в Library.

Все функции — синхронные (CPU/IO-bound): бот вызывает их через
``run_in_executor``, чтобы не блокировать event loop.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

import httpx
from mutagen.flac import FLAC

from covers import CoverCandidate, download_cover, search_covers, is_valid_cover_candidate
from sanitizer import TagSanitizer
from surgeon import FLACSurgeon, clean_date_str, extract_year_and_date, parse_genres
from renamer import FileSystemManager
from lyricist import LyricManager, clean_track_title

log = logging.getLogger(__name__)


# ── Определение альбома для одиночных треков ─────────────────────────────────

def resolve_track_parent_album(artist: str, title: str) -> dict | None:
    """Определяет родительский полноформатный альбом/EP и канонического Album Artist онлайн.

    Ищет трек через iTunes API и Deezer API. Если трек входит в полноформатный альбом (LP/EP),
    выбирает оригинальный студийный альбом (отдавая предпочтение стандартным изданиям над Deluxe
    и отсекая сборники лучших хитов). Если трек выходил только синглом, возвращает сингл.
    """
    cl_t = clean_track_title(title)
    if not cl_t:
        return None

    artist_tokens = [a.strip() for a in re.split(r"[,;&]|\s+(?:feat\.?|ft\.?|x|and)\s+", artist, flags=re.I) if a.strip()]
    if not artist_tokens:
        return None

    queries = [f"{artist_tokens[0]} {cl_t}"]
    if len(artist_tokens) >= 2:
        queries.insert(0, f"{artist_tokens[0]} {artist_tokens[1]} {cl_t}")
    if f"{artist} {cl_t}" not in queries:
        queries.append(f"{artist} {cl_t}")

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    seen_ids: set[int] = set()
    raw_candidates: list[dict] = []

    def _norm(s: str) -> str:
        return re.sub(r"[^\w]", "", (s or "").lower())

    target_norm_t = _norm(cl_t)
    clean_target_arts = [_norm(a) for a in artist_tokens if len(_norm(a)) > 1]

    # 1. Поиск в iTunes Search API
    for q in queries[:3]:
        for country in [None, "US"]:
            try:
                params = {"term": q, "entity": "song", "limit": 15}
                if country:
                    params["country"] = country
                r = httpx.get("https://itunes.apple.com/search", params=params, headers=headers, timeout=5.0)
                if r.status_code != 200:
                    continue
                for item in r.json().get("results", []):
                    trk_id = item.get("trackId")
                    if trk_id in seen_ids:
                        continue
                    seen_ids.add(trk_id)

                    t_name = item.get("trackName", "")
                    cand_norm_t = _norm(clean_track_title(t_name))
                    if cand_norm_t != target_norm_t and SequenceMatcher(None, target_norm_t, cand_norm_t).ratio() < 0.85:
                        continue

                    cand_art = item.get("artistName", "")
                    cand_coll_art = item.get("collectionArtistName") or cand_art
                    combined_cand_art = _norm(cand_art + " " + cand_coll_art)

                    # Отсекаем караоке, трибьюты, колыбельные
                    if any(k in combined_cand_art or k in _norm(t_name) for k in ["karaoke", "tribute", "originallyperformed", "instrumentalversion", "lullaby"]):
                        continue

                    # Проверяем совпадение хотя бы одного артиста трека
                    if not any(a in combined_cand_art for a in clean_target_arts):
                        continue

                    coll_name = item.get("collectionName", "")
                    if not coll_name:
                        continue

                    cnt = item.get("trackCount", 1)
                    num = item.get("trackNumber", 1)
                    date_str = item.get("releaseDate", "")
                    genre_name = item.get("primaryGenreName", "")

                    is_single = cnt <= 2 or coll_name.lower().endswith("- single") or "(single)" in coll_name.lower()

                    raw_candidates.append({
                        "album": coll_name,
                        "album_artist": cand_coll_art,
                        "track_number": num,
                        "track_total": cnt,
                        "release_date": date_str,
                        "genre": genre_name,
                        "is_single": is_single,
                    })
            except Exception as e:
                log.debug("iTunes track lookup error: %s", e)

    # 2. Deezer API (fallback для треков, не найденных в iTunes как полноформатный альбом)
    if not any(not c["is_single"] for c in raw_candidates):
        for q in [f'track:"{cl_t}" artist:"{artist_tokens[0]}"', f"{artist_tokens[0]} {cl_t}"]:
            try:
                r = httpx.get("https://api.deezer.com/search", params={"q": q}, headers=headers, timeout=5.0)
                if r.status_code != 200:
                    continue
                for d in r.json().get("data", []):
                    t_name = d.get("title", "")
                    cand_norm_t = _norm(clean_track_title(t_name))
                    if cand_norm_t != target_norm_t and SequenceMatcher(None, target_norm_t, cand_norm_t).ratio() < 0.85:
                        continue
                    d_art = d.get("artist", {}).get("name", "")
                    if not any(a in _norm(d_art) for a in clean_target_arts):
                        continue
                    d_alb_id = d.get("album", {}).get("id")
                    if d_alb_id:
                        try:
                            alb_r = httpx.get(f"https://api.deezer.com/album/{d_alb_id}", headers=headers, timeout=4.0).json()
                            nb = alb_r.get("nb_tracks", 1)
                            rec_type = alb_r.get("record_type", "album")
                            aa = alb_r.get("artist", {}).get("name") or d_art
                            genres_raw = alb_r.get("genres", {}).get("data", [])
                            genre_cand = genres_raw[0].get("name", "") if genres_raw else ""
                            raw_candidates.append({
                                "album": alb_r.get("title") or d.get("album", {}).get("title", ""),
                                "album_artist": aa,
                                "track_number": 1,
                                "track_total": nb,
                                "release_date": alb_r.get("release_date", ""),
                                "genre": genre_cand,
                                "is_single": nb <= 2 or rec_type == "single" or "single" in str(alb_r.get("title", "")).lower(),
                            })
                        except Exception:
                            pass
            except Exception as e:
                log.debug("Deezer track lookup error: %s", e)

    if not raw_candidates:
        return None

    albums = [c for c in raw_candidates if not c["is_single"]]
    singles = [c for c in raw_candidates if c["is_single"]]

    if albums:
        # Сортировка альбомов: отсекаем сборники хитов, предпочитаем стандартное издание,
        # совпадение с основным артистом и наиболее раннюю дату релиза
        def album_sort_key(c: dict) -> tuple:
            name = c["album"].lower()
            is_comp = any(k in name for k in ["highlights", "greatest hits", "best of", "anthology", "collection", "playlist", "platinum", "gold"])
            is_deluxe = any(k in name for k in ["deluxe", "expanded", "remix", "anniversary", "instrumentals", "edition"])
            aa_norm = _norm(c["album_artist"])
            matches_primary = clean_target_arts[0] in aa_norm if clean_target_arts else False
            is_eponymous = target_norm_t in _norm(c["album"])
            date_key = c["release_date"][:10] if c.get("release_date") else "9999-99-99"
            return (
                0 if not is_comp else 1,
                0 if not is_deluxe else 1,
                0 if is_eponymous else 1,
                0 if matches_primary else 1,
                date_key,
                -c["track_total"],
            )
        chosen = sorted(albums, key=album_sort_key)[0]
    else:
        chosen = singles[0]
        chosen["album"] = re.sub(r"(?i)\s*[\(\[](?:feat\.?|ft\.?|with|prod\.?|single).*?[\)\]]", "", chosen["album"])
        chosen["album"] = re.sub(r"(?i)\s*-\s*single$", "", chosen["album"]).strip()

    return chosen


def enrich_single_track_release(folder: Path) -> dict | None:
    """Для релиза из 1 трека определяет родительский альбом и канонического Album Artist онлайн.

    Если трек выходил в составе полноформатного альбома или EP, обновляет теги
    ALBUM, ALBUMARTIST, TRACKNUMBER, TRACKTOTAL (и DATE при необходимости),
    чтобы релиз корректно оформлялся как альбом основного исполнителя.
    Если трек выходил только синглом, оставляет статус сингла.
    """
    flac_files = sorted(folder.rglob("*.flac"))
    if len(flac_files) != 1:
        return None

    flac_path = flac_files[0]
    try:
        audio = FLAC(str(flac_path))
    except Exception:
        return None

    title = audio.get("title", [""])[0].strip()
    arts = [a.strip() for a in audio.get("artist", []) if a.strip()]
    if not arts:
        arts = [a.strip() for a in audio.get("albumartist", []) if a.strip()]
    artist_str = ", ".join(arts) if arts else ""

    if not artist_str or not title:
        name = flac_path.stem
        if " - " in name:
            parts = name.split(" - ", 1)
            artist_str = artist_str or parts[0].strip()
            title = title or parts[1].strip()
        elif " - " in folder.name:
            parts = folder.name.split(" - ", 1)
            artist_str = artist_str or parts[0].strip()
            title = title or parts[1].strip()

    if not artist_str or not title:
        return None

    # Проверяем, нужна ли онлайн-проверка:
    # если уже есть 1 альбомный артист, альбом отличается от названия трека,
    # не содержит "single" и трек-тотал > 2 — повторный сетевой запрос не нужен.
    cur_aa = audio.get("albumartist", [])
    cur_album = audio.get("album", [""])[0].strip()
    cur_tot = int(audio.get("tracktotal", ["0"])[0] if audio.get("tracktotal", ["0"])[0].isdigit() else 0)
    if (
        len(cur_aa) == 1
        and cur_album
        and cur_album.lower() != title.lower()
        and "single" not in cur_album.lower()
        and cur_tot > 2
    ):
        return None

    info = resolve_track_parent_album(artist_str, title)
    if not info:
        return None

    changed = False

    # 1. ALBUM
    new_album = info.get("album", "").strip()
    if new_album:
        cur_album = audio.get("album", [""])[0].strip()
        if not cur_album or cur_album.lower() == title.lower() or "single" in cur_album.lower() or not info.get("is_single"):
            audio["album"] = [new_album]
            changed = True

    # 2. ALBUMARTIST
    new_aa = info.get("album_artist", "").strip()
    if new_aa:
        sanitizer = TagSanitizer()
        canonical_aa = sanitizer.process_artists(new_aa)
        if canonical_aa:
            audio["albumartist"] = canonical_aa
            changed = True

    # 3. TRACKNUMBER & TRACKTOTAL
    num = info.get("track_number")
    tot = info.get("track_total")
    cur_num = audio.get("tracknumber", [""])[0].strip()
    if num and (not cur_num or cur_num in ("0", "1", "01")):
        audio["tracknumber"] = [str(num)]
        changed = True
    if tot:
        audio["tracktotal"] = [str(tot)]
        changed = True

    # 4. DATE & ORIGINALDATE
    rel_date = info.get("release_date", "").strip()
    if rel_date:
        year_4, full_d = extract_year_and_date(rel_date)
        if year_4:
            audio["date"] = [year_4]
            audio["originaldate"] = [full_d]
            changed = True

    if "year" in audio:
        del audio["year"]
        changed = True

    # 5. GENRE
    cand_genre = info.get("genre", "")
    if cand_genre:
        existing_g = audio.get("genre", [])
        if not existing_g:
            parsed = parse_genres(cand_genre)
            if parsed:
                audio["genre"] = parsed
                changed = True

    if changed:
        audio.save()
        log.info(
            "Одиночный трек онлайн-привязан к альбому: %s — %s (AlbumArtist: %s, трек %s/%s)",
            artist_str,
            audio.get("album", [""])[0],
            audio.get("albumartist", [""])[0],
            audio.get("tracknumber", [""])[0],
            audio.get("tracktotal", [""])[0],
        )

    return info


# ── Информация об альбоме ────────────────────────────────────────────────────

def album_info(folder: Path) -> tuple[str, str]:
    """Возвращает (artist, album) релиза.

    Учитывает всех артистов альбома (из ALBUMARTIST или общих артистов треков),
    не отсекая соавторов релиза. Название альбома определяется по консенсусу треков (Counter).
    """
    flac_files = sorted(folder.rglob("*.flac"))
    if not flac_files:
        return folder.name, ""

    album_names: list[str] = []
    album_artists_list: list[tuple[str, ...]] = []
    track_artists_sets: list[set[str]] = []
    first_track_artists: list[str] = []

    for f in flac_files:
        try:
            audio = FLAC(str(f))
            al = audio.get("album", [""])[0].strip()
            if al:
                album_names.append(al)

            aa = [a.strip() for a in audio.get("albumartist", []) if a.strip()]
            if aa:
                album_artists_list.append(tuple(aa))

            a_list = [a.strip() for a in audio.get("artist", []) if a.strip()]
            if a_list:
                if not first_track_artists:
                    first_track_artists = a_list
                track_artists_sets.append(set(a_list))
        except Exception:
            continue

    if len(flac_files) == 1 and not album_artists_list:
        try:
            enrich_single_track_release(folder)
            audio = FLAC(str(flac_files[0]))
            al = audio.get("album", [""])[0].strip()
            if al:
                album_names = [al]
            aa = [a.strip() for a in audio.get("albumartist", []) if a.strip()]
            if aa:
                album_artists_list = [tuple(aa)]
        except Exception:
            pass

    folder_album_hint = ""
    folder_artist_hint = ""
    if " - " in folder.name:
        parts = folder.name.split(" - ", 1)
        folder_artist_hint = parts[0].strip()
        folder_album_hint = parts[1].strip()

    album_name = ""
    if album_names:
        counts = Counter(album_names)
        most_common = counts.most_common()
        top_name, top_count = most_common[0]
        # Если есть явный лидер (> половины треков), берём его
        if top_count > len(album_names) / 2:
            album_name = top_name
        else:
            # Ничья или разброд тегов: проверяем, совпадает ли какой-то из кандидатов с именем папки
            if folder_album_hint:
                norm_hint = re.sub(r"[^\w]", "", folder_album_hint.lower())
                for cand, _ in most_common:
                    if re.sub(r"[^\w]", "", cand.lower()) == norm_hint:
                        album_name = cand
                        break
            if not album_name:
                album_name = top_name
    elif folder_album_hint:
        album_name = folder_album_hint
    else:
        album_name = folder.name

    if album_artists_list:
        best_aa = list(Counter(album_artists_list).most_common(1)[0][0])
        if len(best_aa) == 2:
            artist_str = " & ".join(best_aa)
        else:
            artist_str = ", ".join(best_aa)
    elif track_artists_sets:
        common = set.intersection(*track_artists_sets)
        if common:
            ordered_common = [a for a in first_track_artists if a in common]
            if not ordered_common:
                ordered_common = sorted(common, key=lambda s: s.casefold())
            if len(ordered_common) == 2:
                artist_str = " & ".join(ordered_common)
            else:
                artist_str = ", ".join(ordered_common)
        else:
            if folder_artist_hint:
                artist_str = folder_artist_hint
            else:
                artist_str = ", ".join(first_track_artists) if first_track_artists else "Various Artists"
    else:
        artist_str = folder_artist_hint or folder.name

    return artist_str, album_name


def count_flacs(folder: Path) -> int:
    """Количество FLAC-файлов в папке (рекурсивно)."""
    return sum(1 for _ in folder.rglob("*.flac"))


# ── Staging: копирование ─────────────────────────────────────────────────────

def copy_to_staging(src: Path, staging_root: Path) -> Path:
    """Копирует папку релиза из Inbox в Staging.

    Если в Staging уже есть папка с таким именем — удаляет старую
    (это повторная обработка того же релиза).

    Returns:
        Путь к копии в Staging.
    """
    dest = staging_root / src.name
    if dest.exists():
        log.info("Staging: удаляю старую копию %s", dest.name)
        shutil.rmtree(dest)

    staging_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    log.info("Staging: скопировано %s → %s", src, dest)
    return dest


# ── Обработка (Apply) ────────────────────────────────────────────────────────

def apply_cover(
    folder: Path,
    cover: CoverCandidate,
) -> str:
    """Скачивает выбранную обложку и записывает дату в теги.

    Returns:
        Краткий отчёт (строка).
    """
    dest = folder / "cover.jpg"
    download_cover(cover.big_url, dest)

    date_note = ""
    if cover.date:
        _apply_date_to_flacs(folder, cover.date)
        date_note = f", дата {cover.date} → теги"

    res_note = f" ({cover.resolution_str})" if cover.resolution_str else ""
    return f"🖼️ Обложка [{cover.source}]{res_note}{date_note}"


def update_album_cover(folder: Path, cover: CoverCandidate) -> str:
    """Заменяет обложку и обновляет дату в уже существующем альбоме в Library."""
    from mutagen.flac import Picture

    dest = folder / "cover.jpg"
    download_cover(cover.big_url, dest)
    with open(dest, "rb") as f:
        img_data = f.read()

    pic = Picture()
    pic.type = 3
    pic.mime = "image/jpeg"
    pic.desc = "Front Cover"
    pic.data = img_data

    flacs = sorted(folder.rglob("*.flac"))
    for f in flacs:
        try:
            audio = FLAC(str(f))
            audio.clear_pictures()
            audio.add_picture(pic)
            if cover.date:
                year_4, full_cov_date = extract_year_and_date(cover.date)
                if year_4:
                    existing_orig = audio.get("originaldate", [""])[0].strip()
                    if not existing_orig or _date_is_more_specific(full_cov_date, existing_orig):
                        audio["originaldate"] = [full_cov_date]
                        audio["date"] = [year_4]
                    elif not audio.get("date"):
                        audio["date"] = [year_4]
            if "year" in audio:
                del audio["year"]
            audio.save()
        except Exception as e:
            log.warning("Не удалось обновить обложку в %s: %s", f.name, e)

    res_note = f" ({cover.resolution_str})" if cover.resolution_str else ""
    return f"🖼️ Обложка [{cover.source}]{res_note} успешно вшита в {len(flacs)} треков"


def apply_pipeline(
    folder: Path,
    *,
    db_path: str = "navidrome.db",
) -> tuple[list[str], Path]:
    """Запускает полный pipeline обработки для всех FLAC в папке.

    Порядок шагов:
    1. surgeon.heal_file — чистка тегов, вшивание cover.jpg, синхронизация дат
    2. renamer.rename_file — переименование трека (01. Название.flac)
    3. lyricist.process_file — скачивание .lrc
    4. renamer.rename_album_folder — переименование папки (Artist - Album)

    Returns:
        Кортеж (список строк-отчёта, обновлённый путь к папке в Staging).
    """
    sanitizer = TagSanitizer(db_path=db_path)
    surgeon = FLACSurgeon(sanitizer=sanitizer)
    renamer = FileSystemManager()
    lyricist = LyricManager()

    report: list[str] = []

    flac_files = sorted(folder.rglob("*.flac"))
    if not flac_files:
        return ["⚠️ FLAC-файлы не найдены в staging"], folder

    if len(flac_files) == 1:
        try:
            enrich_single_track_release(folder)
        except Exception as e:
            log.warning("Ошибка enrich_single_track_release: %s", e)

    for flac in flac_files:
        surgeon.heal_file(str(flac))

    # Онлайн-обогащение метаданных (Explicit, Label, Genre, Date, Track numbers/totals)
    try:
        enrich_album_online_metadata(folder, force=False)
    except Exception as e:
        log.warning("Ошибка enrich_album_online_metadata: %s", e)

    # Переименование файлов и скачивание текстов песен
    flac_files = sorted(folder.rglob("*.flac"))
    for flac in flac_files:
        new_path = renamer.rename_file(str(flac))
        lrc_status = lyricist.process_file(new_path)
        track_artist_str = ""
        try:
            audio = FLAC(str(new_path))
            arts = audio.get("artist", [])
            if arts:
                track_artist_str = f" (👤 {', '.join(arts)})"
        except Exception:
            pass
        report.append(f"• `{Path(new_path).name}`{track_artist_str} — {lrc_status}")

    # Очистка осиротевших .lrc файлов (для которых нет соответствующего .flac)
    current_flac_stems = {f.stem for f in folder.rglob("*.flac")}
    for lrc_f in folder.rglob("*.lrc"):
        if lrc_f.stem not in current_flac_stems:
            try:
                lrc_f.unlink()
                log.info("Удалён осиротевший .lrc файл: %s", lrc_f.name)
            except Exception as e:
                log.warning("Не удалось удалить осиротевший .lrc %s: %s", lrc_f.name, e)

    # Переименовываем папку альбома последней (пути выше уже отработали).
    new_folder = Path(renamer.rename_album_folder(str(folder)))
    report.append(f"📁 `{new_folder.name}`")

    # Расчёт ReplayGain (потрековый и альбомный)
    from replaygain import apply_replaygain
    rg_ok, rg_msg = apply_replaygain(new_folder)
    if rg_ok:
        report.append("🔊 ReplayGain: рассчитан (Track & Album Gain)")
    else:
        report.append(f"⚠️ ReplayGain: {rg_msg}")

    return report, new_folder


# ── Перенос в Library ────────────────────────────────────────────────────────

def find_existing_library_album(staging_folder: Path, library_root: Path) -> Path | None:
    """Ищет существующую папку альбома в Library для слияния.

    Проверяет:
    1. Точное совпадение имени папки ('Artist - Album').
    2. Регистронезависимое совпадение имени папки.
    3. Совпадение по тегам ARTIST и ALBUM.
    """
    if not library_root.exists():
        return None

    # 1. Точное совпадение
    exact = library_root / staging_folder.name
    if exact.exists() and exact.is_dir():
        return exact

    # 2. Регистронезависимое совпадение
    st_name_lower = staging_folder.name.lower()
    for existing in library_root.iterdir():
        if existing.is_dir() and existing.name.lower() == st_name_lower:
            return existing

    # 3. Совпадение по тегам ARTIST и ALBUM
    st_artist, st_album = album_info(staging_folder)
    if st_artist and st_album:
        norm_art = st_artist.lower().strip()
        norm_alb = st_album.lower().strip()
        expected_prefix = f"{norm_art} - {norm_alb}"

        for existing in library_root.iterdir():
            if not existing.is_dir():
                continue
            ex_name = existing.name.lower()
            if ex_name.startswith(expected_prefix) or (norm_art in ex_name and norm_alb in ex_name):
                ex_art, ex_alb = album_info(existing)
                if ex_art.lower().strip() == norm_art and ex_alb.lower().strip() == norm_alb:
                    return existing

    return None


def move_to_library(
    staging_folder: Path,
    library_root: Path,
    has_chosen_cover: bool = False,
) -> tuple[Path, dict]:
    """Переносит обработанную папку из Staging в Library.

    Если альбом уже есть в Library — объединяет файлы в существующую папку
    без создания дубликата с суффиксом (2), вшивает обложку в новые треки
    и пересчитывает единый ReplayGain для всего объединённого релиза.

    Returns:
        Кортеж (путь к целевой папке в Library, словарь со статистикой слияния).
    """
    library_root.mkdir(parents=True, exist_ok=True)
    existing_dest = find_existing_library_album(staging_folder, library_root)

    renamer = FileSystemManager()

    if existing_dest is not None:
        log.info("Library: найден существующий альбом '%s'. Запускаю слияние...", existing_dest.name)
        info = renamer.merge_folders(staging_folder, existing_dest, has_chosen_cover=has_chosen_cover)
        info["merged"] = True
        return existing_dest, info

    dest = library_root / staging_folder.name
    shutil.move(str(staging_folder), str(dest))
    log.info("Library: перенесено %s → %s", staging_folder.name, dest)
    all_flacs = list(dest.rglob("*.flac"))
    return dest, {
        "merged": False,
        "added_tracks": [f.name for f in all_flacs],
        "updated_tracks": [],
        "total_flacs": len(all_flacs),
        "rg_ok": True,
        "rg_msg": "OK",
    }


def cleanup_inbox(inbox_folder: Path) -> None:
    """Удаляет оригинальную папку из Inbox после успешного переноса в Library."""
    if inbox_folder.exists():
        shutil.rmtree(inbox_folder)
        log.info("Inbox: удалено %s", inbox_folder.name)


# ── Хелперы ──────────────────────────────────────────────────────────────────

def _date_is_more_specific(new_date: str, existing_date: str) -> bool:
    """True, если *new_date* содержит больше информации, чем *existing_date*.

    Критерий: длина строки. "2019-06-21" (10) > "2019" (4).
    """
    return len(new_date.strip()) > len(existing_date.strip())


def _apply_date_to_flacs(folder: Path, date_str: str) -> None:
    """Записывает год в DATE и полную дату в ORIGINALDATE всех FLAC в папке."""
    year_4, full_d = extract_year_and_date(date_str)
    if not year_4:
        return

    for f in folder.rglob("*.flac"):
        try:
            audio = FLAC(str(f))
            existing_orig = audio.get("originaldate", [""])[0].strip()
            existing_date = audio.get("date", [""])[0].strip()

            changed = False
            if "year" in audio:
                del audio["year"]
                changed = True

            if not existing_orig or _date_is_more_specific(full_d, existing_orig):
                audio["originaldate"] = [full_d]
                audio["date"] = [year_4]
                changed = True
            elif not existing_date:
                audio["date"] = [year_4]
                changed = True

            if changed:
                audio.save()
        except Exception as e:
            log.warning("Не удалось записать дату в %s: %s", f.name, e)


# ── Управление артистами ─────────────────────────────────────────────────────

def get_album_artists_info(folder: Path) -> dict:
    """Возвращает информацию об артистах релиза и его треков."""
    flac_files = sorted(folder.rglob("*.flac"))
    album_artist_val = ""
    album_title = ""
    tracks_info = []

    for f in flac_files:
        try:
            audio = FLAC(str(f))
            if not album_artist_val:
                album_artist_val = ", ".join(audio.get("albumartist", []))
            if not album_title:
                album_title = audio.get("album", [""])[0]

            track_num = audio.get("tracknumber", [""])[0].split("/")[0]
            title = audio.get("title", [f.stem])[0]
            artists = audio.get("artist", [])

            tracks_info.append({
                "path": f,
                "track_num": track_num,
                "title": title,
                "artists": artists,
            })
        except Exception:
            continue

    return {
        "folder": folder,
        "album": album_title or folder.name,
        "album_artist": album_artist_val or "—",
        "tracks": tracks_info,
    }


def get_album_artist_suggestions(folder: Path) -> list[str]:
    """Формирует список рекомендуемых вариантов ALBUMARTIST на основе треков альбома."""
    info = get_album_artists_info(folder)
    tracks = info.get("tracks", [])
    if not tracks:
        return []

    track_artist_sets = [set(t["artists"]) for t in tracks if t["artists"]]
    all_unique: list[str] = []
    for t in tracks:
        for a in t["artists"]:
            if a not in all_unique:
                all_unique.append(a)

    suggestions: list[str] = []

    # 1. Все уникальные исполнители треков (если их до 4)
    if all_unique and len(all_unique) <= 4:
        all_str = ", ".join(all_unique)
        if all_str not in suggestions:
            suggestions.append(all_str)

    # 2. Общие артисты для всех треков (если отличаются от всех уникальных)
    if track_artist_sets:
        common = set.intersection(*track_artist_sets)
        if common:
            common_ordered = [a for a in all_unique if a in common]
            common_str = ", ".join(common_ordered)
            if common_str and common_str not in suggestions:
                suggestions.append(common_str)

    # 3. Текущий ALBUMARTIST (если уже есть и не в списке)
    current_aa = info.get("album_artist", "")
    if current_aa and current_aa != "—" and current_aa not in suggestions:
        suggestions.append(current_aa)

    # 4. Если на треках больше 2 разных артистов — предложить "Various Artists"
    if len(tracks) > 1 and len(all_unique) > 2 and "Various Artists" not in suggestions:
        suggestions.append("Various Artists")

    return suggestions


def add_track_artist(track_path: Path, new_artist: str) -> list[str]:
    """Добавляет артиста в тег ARTIST трека (ручной ввод не разбивается)."""
    raw = new_artist.strip()
    if not raw or not track_path.exists():
        return []

    # Если пользователь явно разделил точкой с запятой ';', добавляем каждого
    if ";" in raw:
        to_add = [a.strip() for a in raw.split(";") if a.strip()]
    else:
        to_add = [raw]

    audio = FLAC(str(track_path))
    artists = list(audio.get("artist", []))
    for art in to_add:
        if art not in artists:
            artists.append(art)
    audio["artist"] = artists
    audio.save()
    return audio.get("artist", [])


def remove_track_artist(track_path: Path, artist_to_remove: str) -> list[str]:
    """Удаляет артиста из тега ARTIST трека."""
    if not track_path.exists():
        return []

    audio = FLAC(str(track_path))
    artists = [a for a in audio.get("artist", []) if a.strip().lower() != artist_to_remove.strip().lower()]
    audio["artist"] = artists
    audio.save()
    return audio.get("artist", [])


def update_album_artist(folder: Path, new_album_artist: str) -> tuple[Path, str]:
    """Обновляет тег ALBUMARTIST у всех треков альбома и переименовывает папку."""
    flac_files = sorted(folder.rglob("*.flac"))
    if not flac_files:
        return folder, "В папке нет FLAC-файлов."

    raw = new_album_artist.strip()
    if raw in ("-", "none", "del", "delete", "clear", ""):
        aa_list: list[str] = []
    elif ";" in raw:
        aa_list = [a.strip() for a in raw.split(";") if a.strip()]
    else:
        aa_list = [raw]

    for f in flac_files:
        try:
            audio = FLAC(str(f))
            if aa_list:
                audio["albumartist"] = aa_list
            else:
                if "albumartist" in audio:
                    del audio["albumartist"]
            audio.save()
        except Exception as e:
            log.warning("Не удалось обновить ALBUMARTIST в %s: %s", f.name, e)

    renamer = FileSystemManager()
    new_folder_str = renamer.rename_album_folder(str(folder))
    new_folder = Path(new_folder_str)

    aa_display = ", ".join(aa_list) if aa_list else "удалён"
    if new_folder.name != folder.name:
        status_str = f"ALBUMARTIST обновлён на: {aa_display}\n📂 Папка переименована в `{new_folder.name}`"
    else:
        status_str = f"ALBUMARTIST обновлён на: {aa_display}"

    return new_folder, status_str


def replace_all_tracks_artist(folder: Path, new_artist: str) -> tuple[Path, str]:
    """Заменяет основного исполнителя во всех треках альбома, обновляет ALBUMARTIST и имя папки."""
    flac_files = sorted(folder.rglob("*.flac"))
    if not flac_files:
        return folder, "В папке нет FLAC-файлов."

    raw = new_artist.strip()
    if not raw:
        return folder, "Имя артиста не может быть пустым."

    # Сохраняем имя ровно так, как ввёл пользователь (без принудительного сплита)
    artists_list = [raw]

    for f in flac_files:
        try:
            audio = FLAC(str(f))
            audio["artist"] = artists_list
            audio["albumartist"] = artists_list
            audio.save()
        except Exception as e:
            log.warning("Не удалось обновить артиста в %s: %s", f.name, e)

    renamer = FileSystemManager()
    new_folder_str = renamer.rename_album_folder(str(folder))
    new_folder = Path(new_folder_str)

    if new_folder.name != folder.name:
        status_str = f"Исполнитель во всех треках: {raw}\n📂 Папка переименована в `{new_folder.name}`"
    else:
        status_str = f"Исполнитель во всех треках: {raw}"

    return new_folder, status_str


def set_track_artists(track_path: Path, new_artists_str: str) -> list[str]:
    """Устанавливает точное имя артиста для одного трека (без ошибочного сплита)."""
    raw = new_artists_str.strip()
    if not raw:
        return []
    if ";" in raw:
        artists_list = [a.strip() for a in raw.split(";") if a.strip()]
    else:
        artists_list = [raw]
    try:
        audio = FLAC(str(track_path))
        audio["artist"] = artists_list
        audio.save()
    except Exception as e:
        log.warning("Не удалось установить артистов для %s: %s", track_path.name, e)
    return artists_list


# ── Определение Explicit, Лейбла, Жанров и Треков онлайн ─────────────────────

@dataclass
class OnlineAlbumInfo:
    explicit_map: dict[str, bool] = field(default_factory=dict)
    track_positions: dict[str, int] = field(default_factory=dict)
    label: str = ""
    genres: list[str] = field(default_factory=list)
    release_date: str = ""
    total_tracks: int = 0


@lru_cache(maxsize=128)
def _fetch_genius_album_tracks_cached(artist: str, album: str) -> tuple[tuple[tuple[str, int], ...], int, str]:
    """Запрашивает треклист альбома из Genius API с кэшированием."""
    if not artist or not album:
        return (), 0, ""

    def norm(s: str) -> str:
        return re.sub(r"[^\w]", "", s.lower().replace("ё", "е"))

    g_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    queries = [f"{artist} {album}", album]
    first_artist = artist.split(",")[0].split("&")[0].strip()
    if first_artist and first_artist != artist:
        queries.insert(1, f"{first_artist} {album}")

    candidates = []
    seen_ids = set()
    norm_alb = norm(album)

    try:
        with httpx.Client(headers=g_headers, timeout=6.0) as client:
            for q in queries:
                try:
                    r = client.get("https://genius.com/api/search/multi", params={"q": q})
                    if r.status_code != 200:
                        continue
                    sections = r.json().get("response", {}).get("sections", [])
                    for s in sections:
                        for h in s.get("hits", []):
                            if h.get("type") == "album" or s.get("type") == "album":
                                res = h.get("result", {})
                                alb_id = res.get("id")
                                if not alb_id or alb_id in seen_ids:
                                    continue
                                seen_ids.add(alb_id)
                                alb_name = res.get("name", "")
                                art_name = res.get("primary_artist_names", "") or res.get("artist", {}).get("name", "")

                                # Фильтруем служебные страницы и переводы Genius
                                lower_alb = alb_name.lower()
                                lower_art = art_name.lower()
                                if any(bad in lower_alb for bad in ["romanized", "translation", "traducción", "tracklist", "credits", "booklet", "liner notes"]):
                                    continue
                                if any(bad in lower_art for bad in ["genius romanizations", "genius translations"]):
                                    continue

                                c_norm_alb = norm(alb_name)
                                if norm_alb not in c_norm_alb and c_norm_alb not in norm_alb:
                                    if SequenceMatcher(None, norm_alb, c_norm_alb).ratio() < 0.65:
                                        continue

                                candidates.append((alb_id, alb_name, art_name, res))
                except Exception as e:
                    log.debug("Ошибка поиска альбома в Genius по запросу '%s': %s", q, e)

                if candidates:
                    break

            best_pos_items: list[tuple[str, int]] = []
            best_total = 0
            best_date = ""

            for alb_id, alb_name, art_name, res in candidates[:3]:
                try:
                    tr_r = client.get(f"https://genius.com/api/albums/{alb_id}/tracks")
                    if tr_r.status_code != 200:
                        continue
                    g_tracks = tr_r.json().get("response", {}).get("tracks", [])
                    if not g_tracks:
                        continue

                    pos_m: dict[str, int] = {}
                    for tr in g_tracks:
                        num = tr.get("number")
                        s_obj = tr.get("song", {})
                        t_title = s_obj.get("title", "")
                        t_feat = s_obj.get("title_with_featured", "")
                        if num:
                            if t_title:
                                pos_m[norm(t_title)] = num
                                base_t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t_title).strip()
                                if base_t:
                                    pos_m[norm(base_t)] = num
                                cl_t = re.sub(r"\s*\([A-Za-z0-9\s\.\-—\'\,\!]+\)$", "", t_title).strip()
                                if cl_t:
                                    pos_m[norm(cl_t)] = num
                            if t_feat:
                                pos_m[norm(t_feat)] = num

                    rel_date = ""
                    rdc = res.get("release_date_components") or {}
                    if rdc.get("year"):
                        y = rdc["year"]
                        m = rdc.get("month")
                        d = rdc.get("day")
                        if m and d:
                            rel_date = f"{y:04d}-{m:02d}-{d:02d}"
                        elif m:
                            rel_date = f"{y:04d}-{m:02d}"
                        else:
                            rel_date = f"{y:04d}"

                    if len(g_tracks) > best_total:
                        best_pos_items = list(pos_m.items())
                        best_total = len(g_tracks)
                        best_date = rel_date
                except Exception as e:
                    log.debug("Ошибка получения треков из Genius для альбома %s: %s", alb_id, e)

            return tuple(best_pos_items), best_total, best_date
    except Exception as e:
        log.debug("Genius API error for '%s — %s': %s", artist, album, e)
        return (), 0, ""


def fetch_genius_album_tracks(artist: str, album: str) -> tuple[dict[str, int], int, str]:
    """Запрашивает треклист альбома из Genius API.
    Возвращает (track_positions, total_tracks, release_date).
    """
    items, total, dt = _fetch_genius_album_tracks_cached(artist, album)
    return dict(items), total, dt


def fetch_album_online_metadata(artist: str, album: str) -> OnlineAlbumInfo:
    """Ищет альбом в Deezer (с fallback на iTunes) и возвращает метаданные релиза."""
    if not artist or not album:
        return OnlineAlbumInfo()

    def norm(s: str) -> str:
        return re.sub(r"[^\w]", "", s.lower().replace("ё", "е"))

    # 1. Deezer API
    queries = [f"{artist} {album}", album]
    first_artist = artist.split(",")[0].split("&")[0].strip()
    if first_artist and first_artist != artist:
        queries.insert(1, f"{first_artist} {album}")

    for q in queries:
        try:
            r = httpx.get(
                "https://api.deezer.com/search/album",
                params={"q": q},
                timeout=4.0,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    alb_id = data[0].get("id")
                    alb_r = httpx.get(
                        f"https://api.deezer.com/album/{alb_id}",
                        timeout=4.0,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    if alb_r.status_code == 200:
                        alb_json = alb_r.json()
                        label = (alb_json.get("label") or "").strip()
                        raw_date = (alb_json.get("release_date") or "").strip()
                        rel_date = clean_date_str(raw_date)
                        nb_tracks = int(alb_json.get("nb_tracks") or 0)
                        raw_genres = alb_json.get("genres", {}).get("data", [])
                        genres_list = parse_genres([g.get("name", "") for g in raw_genres if g.get("name")])

                        tracks = alb_json.get("tracks", {}).get("data", [])
                        m: dict[str, bool] = {}
                        pos_m: dict[str, int] = {}
                        for idx, tr in enumerate(tracks, 1):
                            t_title = tr.get("title", "")
                            t_short = tr.get("title_short", t_title)
                            is_exp = bool(tr.get("explicit_lyrics"))
                            pos = tr.get("track_position") or idx
                            m[norm(t_title)] = is_exp
                            m[norm(t_short)] = is_exp
                            pos_m[norm(t_title)] = pos
                            pos_m[norm(t_short)] = pos
                            base_t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t_title).strip()
                            if base_t:
                                pos_m[norm(base_t)] = pos

                        if m or label or rel_date or genres_list:
                            return OnlineAlbumInfo(
                                explicit_map=m,
                                track_positions=pos_m,
                                label=label,
                                genres=genres_list,
                                release_date=rel_date,
                                total_tracks=nb_tracks,
                            )
        except Exception as e:
            log.debug("Ошибка запроса метаданных из Deezer для '%s': %s", q, e)

    # 2. iTunes API fallback
    try:
        r = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {album}", "entity": "album", "limit": 1},
            timeout=4.0,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200:
            res = r.json().get("results", [])
            if res:
                coll_id = res[0].get("collectionId")
                raw_c = (res[0].get("copyright") or "").strip()
                clean_lbl = re.sub(r"(?i)^[℗p©c\s\d\-–/]+", "", raw_c).strip()
                raw_date = (res[0].get("releaseDate") or "").strip()
                rel_date = clean_date_str(raw_date)
                nb_tracks = int(res[0].get("trackCount") or 0)
                genre_name = (res[0].get("primaryGenreName") or "").strip()
                genres_list = parse_genres(genre_name) if genre_name else []

                r2 = httpx.get(
                    "https://itunes.apple.com/lookup",
                    params={"id": coll_id, "entity": "song"},
                    timeout=4.0,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                m = {}
                pos_m = {}
                if r2.status_code == 200:
                    for tr in r2.json().get("results", []):
                        if tr.get("wrapperType") == "track":
                            t_title = tr.get("trackName", "")
                            is_exp = (tr.get("trackExplicitness") == "explicit")
                            pos = tr.get("trackNumber") or 0
                            m[norm(t_title)] = is_exp
                            if pos:
                                pos_m[norm(t_title)] = pos
                                base_t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t_title).strip()
                                if base_t:
                                    pos_m[norm(base_t)] = pos

                if m or clean_lbl or rel_date or genres_list:
                    return OnlineAlbumInfo(
                        explicit_map=m,
                        track_positions=pos_m,
                        label=clean_lbl,
                        genres=genres_list,
                        release_date=rel_date,
                        total_tracks=nb_tracks,
                    )
    except Exception as e:
        log.debug("Ошибка запроса метаданных из iTunes для '%s — %s': %s", artist, album, e)

    # 3. Genius API fallback (для релизов, не найденных в Deezer/iTunes)
    g_pos, g_tot, g_date = fetch_genius_album_tracks(artist, album)
    if g_pos:
        log.info(
            "Genius fallback: найден альбом '%s — %s' (треков: %d)",
            artist,
            album,
            g_tot,
        )
        return OnlineAlbumInfo(
            track_positions=g_pos,
            release_date=g_date,
            total_tracks=g_tot,
        )

    return OnlineAlbumInfo()


def enrich_album_online_metadata(folder: Path, force: bool = False) -> tuple[int, str]:
    """Определяет explicit-статус треков, лейбл, жанры, дату релиза и номера треков через Deezer/iTunes."""
    flac_files = sorted(folder.rglob("*.flac"))
    if not flac_files:
        return 0, ""

    artist, album = album_info(folder)
    if not artist or not album:
        return 0, ""

    info = fetch_album_online_metadata(artist, album)

    # Genius-фоллбэк для треклиста:
    # 1) если у Deezer/iTunes нет track_positions
    # 2) если на стримингах меньше треков, чем файлов в папке (усечённый стриминг-релиз)
    if not info.track_positions or info.total_tracks < len(flac_files):
        g_pos, g_total, g_date = fetch_genius_album_tracks(artist, album)
        if g_pos:
            if not info.track_positions or (g_total >= len(flac_files) > info.total_tracks):
                log.info(
                    "Genius треклист выбран как более полный (%d треков против %d в стриминге): '%s — %s'",
                    g_total,
                    info.total_tracks,
                    artist,
                    album,
                )
                info.track_positions = g_pos
                info.total_tracks = max(info.total_tracks, g_total)
            else:
                for k, v in g_pos.items():
                    if k not in info.track_positions:
                        info.track_positions[k] = v
                info.total_tracks = max(info.total_tracks, g_total)
            if not info.release_date and g_date:
                info.release_date = g_date

    def norm(s: str) -> str:
        return re.sub(r"[^\w]", "", s.lower().replace("ё", "е"))

    def track_num_from_filename(filename: str) -> int | None:
        m = re.match(r"^(\d{1,3})[\s\.\-_]", filename)
        if m:
            return int(m.group(1))
        return None

    marked_count = 0
    label_applied = ""
    genre_applied: list[str] = []
    date_applied = ""

    # ── Подготовка глобального бесколлизионного распределения номеров треков ──
    files_data = []
    for idx, f in enumerate(flac_files, 1):
        try:
            audio = FLAC(str(f))
            t_cand = audio.get("title", [""])[0].strip() or f.stem
            t_clean = re.sub(r"^\d{1,3}[\s\.\-_]+", "", t_cand).strip()
            raw_tn = audio.get("tracknumber", ["0"])[0].strip().split("/")[0].strip()
            ex_num = int(raw_tn) if raw_tn.isdigit() and int(raw_tn) > 0 else None
            fn_n = track_num_from_filename(f.name)
            files_data.append({
                "file": f,
                "audio": audio,
                "idx": idx,
                "title_cand": t_cand,
                "title_clean": t_clean,
                "existing_num": ex_num,
                "fn_num": fn_n,
            })
        except Exception as e:
            log.warning("Ошибка чтения %s: %s", f.name, e)

    matched_pos: dict[Path, tuple[int, float]] = {}
    pos_to_file: dict[int, tuple[Path, float]] = {}

    if info.track_positions:
        for fd in files_data:
            f = fd["file"]
            t_clean = fd["title_clean"]
            t_cand = fd["title_cand"]
            clean_t = re.sub(r"(?i)\s*[\(\[][^()\[\]]*(?:feat|ft)[^()\[\]]*[\)\]]", "", t_clean).strip()
            base_t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t_clean).strip()

            best_p = None
            best_sc = 0.0

            for cand_t in (t_clean, t_cand, clean_t, base_t):
                c_norm = norm(cand_t)
                if c_norm in info.track_positions:
                    best_p = info.track_positions[c_norm]
                    best_sc = 1.0
                    break

            if not best_p and norm(base_t):
                norm_b = norm(base_t)
                for cand_norm, cand_pos in info.track_positions.items():
                    r = SequenceMatcher(None, norm_b, cand_norm).ratio()
                    if r >= 0.85 and r > best_sc:
                        best_sc = r
                        best_p = cand_pos

            if best_p:
                if best_p in pos_to_file:
                    prev_f, prev_sc = pos_to_file[best_p]
                    if best_sc > prev_sc:
                        del matched_pos[prev_f]
                        matched_pos[f] = (best_p, best_sc)
                        pos_to_file[best_p] = (f, best_sc)
                else:
                    matched_pos[f] = (best_p, best_sc)
                    pos_to_file[best_p] = (f, best_sc)

    # Если после первого сопоставления остались файлы без номера, проверяем Genius
    if len(matched_pos) < len(files_data):
        g_pos, g_total, g_date = fetch_genius_album_tracks(artist, album)
        if g_pos:
            for fd in files_data:
                f = fd["file"]
                if f in matched_pos:
                    continue
                t_clean = fd["title_clean"]
                t_cand = fd["title_cand"]
                clean_t = re.sub(r"(?i)\s*[\(\[][^()\[\]]*(?:feat|ft)[^()\[\]]*[\)\]]", "", t_clean).strip()
                base_t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t_clean).strip()

                best_p = None
                best_sc = 0.0

                for cand_t in (t_clean, t_cand, clean_t, base_t):
                    c_norm = norm(cand_t)
                    if c_norm in g_pos:
                        best_p = g_pos[c_norm]
                        best_sc = 1.0
                        break

                if not best_p and norm(base_t):
                    norm_b = norm(base_t)
                    for cand_norm, cand_pos in g_pos.items():
                        r = SequenceMatcher(None, norm_b, cand_norm).ratio()
                        if r >= 0.85 and r > best_sc:
                            best_sc = r
                            best_p = cand_pos

                if best_p and best_p not in pos_to_file:
                    matched_pos[f] = (best_p, best_sc)
                    pos_to_file[best_p] = (f, best_sc)

            if g_total > info.total_tracks:
                info.total_tracks = g_total

    claimed_set = set(pos_to_file.keys())
    final_track_numbers: dict[Path, int] = {}

    for f, (p, _) in matched_pos.items():
        final_track_numbers[f] = p

    # Шаг 2: Для файлов без онлайн-совпадений используем existing_num или fn_num, если нет коллизий
    unmatched = [fd for fd in files_data if fd["file"] not in final_track_numbers]
    still_unmatched = []
    for fd in unmatched:
        cand_num = fd["existing_num"] or fd["fn_num"]
        if cand_num and cand_num > 0 and cand_num not in claimed_set:
            final_track_numbers[fd["file"]] = cand_num
            claimed_set.add(cand_num)
        else:
            still_unmatched.append(fd)

    # Шаг 3: Всем оставшимся трекам назначаем наименьшие свободные слоты 1, 2, 3...
    next_slot = 1
    for fd in still_unmatched:
        while next_slot in claimed_set:
            next_slot += 1
        final_track_numbers[fd["file"]] = next_slot
        claimed_set.add(next_slot)

    max_assigned = max(final_track_numbers.values()) if final_track_numbers else 0
    calculated_total_tracks = max(info.total_tracks or 0, max_assigned, len(flac_files))

    for fd in files_data:
        f = fd["file"]
        audio = fd["audio"]
        changed = False

        try:
            # 1. Простановка лейбла
            if info.label:
                existing_label = audio.get("label", [""])[0].strip()
                if not existing_label or force:
                    audio["label"] = [info.label]
                    changed = True
                    label_applied = info.label

            # 2. Простановка ITUNESADVISORY (Explicit)
            if force or "itunesadvisory" not in audio:
                title = fd["title_cand"]
                clean_t = re.sub(r"(?i)\s*[\(\[][^()\[\]]*(?:feat|ft)[^()\[\]]*[\)\]]", "", title).strip()

                is_exp = info.explicit_map.get(norm(title)) or info.explicit_map.get(norm(clean_t))
                if is_exp:
                    audio["itunesadvisory"] = ["1"]
                    changed = True
                    marked_count += 1
                elif is_exp is False and force:
                    audio["itunesadvisory"] = ["0"]
                    changed = True

            # 3. Простановка жанров (GENRE)
            if info.genres:
                existing_genre = audio.get("genre", [])
                if not existing_genre or force:
                    audio["genre"] = info.genres
                    changed = True
                    genre_applied = info.genres
            elif "genre" in audio:
                cur_parsed = parse_genres(audio["genre"])
                if cur_parsed and cur_parsed != audio["genre"]:
                    audio["genre"] = cur_parsed
                    changed = True

            # 4. Простановка даты релиза (DATE: YYYY, ORIGINALDATE: YYYY-MM-DD или YYYY)
            if info.release_date:
                year_4, full_d = extract_year_and_date(info.release_date)
                if year_4:
                    existing_orig = audio.get("originaldate", [""])[0].strip()
                    existing_date = audio.get("date", [""])[0].strip()
                    if not existing_orig or force or _date_is_more_specific(full_d, existing_orig):
                        audio["originaldate"] = [full_d]
                        audio["date"] = [year_4]
                        changed = True
                        date_applied = full_d
                    elif not existing_date:
                        audio["date"] = [year_4]
                        changed = True

            if "year" in audio:
                del audio["year"]
                changed = True

            # 5. Простановка TRACKTOTAL
            existing_tot = audio.get("tracktotal", ["0"])[0].strip().split("/")[0].strip()
            if not existing_tot or existing_tot == "0" or force or existing_tot != str(calculated_total_tracks):
                audio["tracktotal"] = [str(calculated_total_tracks)]
                changed = True

            # 6. Простановка TRACKNUMBER (строго уникальный без дубликатов)
            target_num = final_track_numbers.get(f)
            if target_num is not None:
                existing_num = audio.get("tracknumber", ["0"])[0].strip().split("/")[0].strip()
                if not existing_num or existing_num == "0" or existing_num != str(target_num) or force:
                    audio["tracknumber"] = [str(target_num)]
                    changed = True

            # 7. Простановка DISCNUMBER и DISCTOTAL
            existing_disc = audio.get("discnumber", ["0"])[0].strip().split("/")[0].strip()
            if not existing_disc or existing_disc == "0":
                audio["discnumber"] = ["1"]
                changed = True
            existing_disctot = audio.get("disctotal", ["0"])[0].strip().split("/")[0].strip()
            if not existing_disctot or existing_disctot == "0":
                audio["disctotal"] = ["1"]
                changed = True

            if changed:
                audio.save()
        except Exception as e:
            log.warning("Ошибка обогащения онлайн-метаданных для %s: %s", f.name, e)

    if marked_count > 0:
        log.info("Проставлен тег ITUNESADVISORY (Explicit) для %d треков в %s", marked_count, folder.name)
    if label_applied:
        log.info("Проставлен тег LABEL ('%s') для треков в %s", label_applied, folder.name)
    if genre_applied:
        log.info("Проставлен тег GENRE (%s) для треков в %s", genre_applied, folder.name)
    if date_applied:
        log.info("Проставлен тег DATE ('%s') для треков в %s", date_applied, folder.name)

    return marked_count, label_applied


def enrich_album_explicit_tags(folder: Path, force: bool = False) -> int:
    """Обратная совместимость: возвращает количество треков с explicit."""
    exp_count, _ = enrich_album_online_metadata(folder, force=force)
    return exp_count


# ── Пакетная идеализация библиотеки ──────────────────────────────────────────

def album_has_cover(album_dir: Path) -> bool:
    """Проверяет наличие обложки в папке альбома или встроенной в FLAC."""
    for ext in ("cover.jpg", "cover.png", "folder.jpg", "folder.png"):
        if (album_dir / ext).exists():
            return True
    for f in sorted(album_dir.rglob("*.flac")):
        try:
            audio = FLAC(str(f))
            if audio.pictures and len(audio.pictures[0].data) > 30_000:
                return True
        except Exception:
            continue
    return False


def upgrade_album(
    album_dir: Path,
    db_path: str | Path = "navidrome.db",
    force_cover: bool = False,
    force_lyrics: bool = False,
    force_replaygain: bool = False,
    force: bool = False,
    cancel_flag: Callable[[], bool] | None = None,
) -> dict:
    """Выполняет полную стандартизацию («идеализацию») альбома на месте.

    1. Очищает и нормализует теги всех треков (FLACSurgeon + TagSanitizer).
    2. Проверяет обложку: если её нет (или force_cover/force=True), ищет и вшивает.
    3. Переименовывает треки в '01. Title.flac' и ищет синхронизированные тексты (.lrc).
    4. Приводит имя папки релиза к 'Artist - Album'.
    5. Рассчитывает ReplayGain (потрековый и альбомный).
    """
    if cancel_flag and cancel_flag():
        return {"cancelled": True, "old_folder": album_dir.name, "new_folder": album_dir.name, "folder_path": album_dir}

    if force:
        force_cover = True
        force_lyrics = True
        force_replaygain = True

    flac_files = sorted(album_dir.rglob("*.flac"))
    if not flac_files:
        return {"error": "no_flacs", "folder": album_dir.name}

    sanitizer = TagSanitizer(db_path=db_path)
    surgeon = FLACSurgeon(sanitizer=sanitizer)
    renamer = FileSystemManager()
    lyricist = LyricManager()

    cover_added = False
    lyrics_added = 0

    # Шаг 1: Первым делом чистим и нормализуем метаданные во всех треках
    for flac in flac_files:
        if cancel_flag and cancel_flag():
            return {"cancelled": True, "old_folder": album_dir.name, "new_folder": album_dir.name, "folder_path": album_dir}
        try:
            surgeon.heal_file(str(flac))
        except Exception as e:
            log.warning("Ошибка очистки тегов в треке %s: %s", flac.name, e)

    # Шаг 1.5: При force-режиме не доверяем рипперам: определяем артистов,
    # которые присутствуют во всех 100% треков релиза, сортируем их по алфавиту
    # и обновляем ALBUMARTIST в тегах всех файлов.
    if force:
        if cancel_flag and cancel_flag():
            return {"cancelled": True, "old_folder": album_dir.name, "new_folder": album_dir.name, "folder_path": album_dir}
        track_artists_sets = []
        for f in flac_files:
            try:
                audio = FLAC(str(f))
                arts = audio.get("artist", [])
                if arts:
                    track_artists_sets.append(set(arts))
            except Exception:
                pass

        if track_artists_sets:
            common = set.intersection(*track_artists_sets)
            if common:
                sorted_common = sorted(common, key=lambda s: s.casefold())
                log.info(
                    "Force upgrade: определены общие артисты релиза (по алфавиту): %s",
                    sorted_common,
                )
                for f in flac_files:
                    try:
                        audio = FLAC(str(f))
                        audio["albumartist"] = sorted_common
                        audio.save()
                    except Exception as e:
                        log.warning("Не удалось обновить ALBUMARTIST в %s: %s", f.name, e)

    # Шаг 1.6: Онлайн-проверка Explicit-статуса и лейбла через Deezer/iTunes
    if cancel_flag and cancel_flag():
        return {"cancelled": True, "old_folder": album_dir.name, "new_folder": album_dir.name, "folder_path": album_dir}
    explicit_added, label_added = enrich_album_online_metadata(album_dir, force=force)

    # Шаг 2: Теперь берём гарантированно чистые artist и album для поиска обложки
    if cancel_flag and cancel_flag():
        return {"cancelled": True, "old_folder": album_dir.name, "new_folder": album_dir.name, "folder_path": album_dir}
    artist, album = album_info(album_dir)
    if force_cover or not album_has_cover(album_dir):
        if artist and album:
            try:
                candidates = search_covers(artist, album)
                if candidates:
                    chosen = candidates[0]
                    if is_valid_cover_candidate(chosen, artist, album):
                        update_album_cover(album_dir, chosen)
                        cover_added = True
                    else:
                        log.warning(
                            "Автоматический подбор обложки для '%s — %s': кандидат '%s — %s' не прошел валидацию названия. Обложка не изменена.",
                            artist, album, chosen.artist, chosen.title
                        )
            except Exception as e:
                log.warning("Не удалось найти обложку для %s — %s: %s", artist, album, e)

    # Шаг 3: Переименование треков и подгрузка текстов песен (.lrc)
    flac_files = sorted(album_dir.rglob("*.flac"))
    for flac in flac_files:
        if cancel_flag and cancel_flag():
            return {"cancelled": True, "old_folder": album_dir.name, "new_folder": album_dir.name, "folder_path": album_dir}
        try:
            new_flac = renamer.rename_file(str(flac))
            res_status = lyricist.process_file(new_flac, overwrite=force_lyrics, smart_upgrade=True)
            if any(k in res_status for k in ("Синхронизированный", "Обычный текст", "LRCLIB", "SyncedLyrics", "Genius", "Обновлено")):
                if "Уже есть" not in res_status and "Сохранено" not in res_status:
                    lyrics_added += 1
        except Exception as e:
            log.warning("Ошибка обработки трека %s: %s", flac.name, e)

    # Очистка осиротевших .lrc файлов (для которых нет соответствующего .flac)
    current_flac_stems = {f.stem for f in album_dir.rglob("*.flac")}
    for lrc_f in album_dir.rglob("*.lrc"):
        if lrc_f.stem not in current_flac_stems:
            try:
                lrc_f.unlink()
                log.info("Удалён осиротевший .lrc файл: %s", lrc_f.name)
            except Exception as e:
                log.warning("Не удалось удалить осиротевший .lrc %s: %s", lrc_f.name, e)

    # Шаг 4: Приведение имени папки к каноническому 'Artist - Album'
    if cancel_flag and cancel_flag():
        return {"cancelled": True, "old_folder": album_dir.name, "new_folder": album_dir.name, "folder_path": album_dir}
    new_folder_str = renamer.rename_album_folder(str(album_dir), force_common_artists=force)
    new_folder = Path(new_folder_str)

    # Шаг 5: Проверка и расчёт ReplayGain
    if cancel_flag and cancel_flag():
        return {"cancelled": True, "old_folder": album_dir.name, "new_folder": new_folder.name, "folder_path": new_folder}
    from replaygain import album_has_replaygain, apply_replaygain
    replaygain_added = False
    if force_replaygain or not album_has_replaygain(new_folder):
        rg_ok, _ = apply_replaygain(new_folder)
        replaygain_added = rg_ok

    return {
        "old_folder": album_dir.name,
        "new_folder": new_folder.name,
        "folder_path": new_folder,
        "tracks_count": len(flac_files),
        "cover_added": cover_added,
        "lyrics_added": lyrics_added,
        "replaygain_added": replaygain_added,
        "explicit_added": explicit_added,
        "label": label_added,
    }


def upgrade_library(
    library_dir: Path,
    db_path: str | Path = "navidrome.db",
    progress_callback: Callable[[int, int, str, dict], None] | None = None,
    cancel_flag: Callable[[], bool] | None = None,
    force: bool = False,
    target_albums: list[Path] | None = None,
) -> dict:
    """Выполняет пакетную стандартизацию («идеализацию») альбомов в Library."""
    if target_albums is not None:
        all_albums = sorted(target_albums)
    else:
        all_albums = sorted([
            d for d in library_dir.iterdir()
            if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.rglob("*.flac") if f.is_file())
        ])

    total = len(all_albums)
    processed = 0
    total_covers = 0
    total_lyrics = 0
    total_rg = 0
    total_explicit = 0
    total_labels = 0
    results = []

    for i, album_folder in enumerate(all_albums, 1):
        if cancel_flag and cancel_flag():
            log.info("Идеализация библиотеки прервана пользователем")
            break

        res = upgrade_album(album_folder, db_path=db_path, force=force, cancel_flag=cancel_flag)
        if res.get("cancelled") or (cancel_flag and cancel_flag()):
            log.info("Идеализация библиотеки прервана пользователем")
            break

        processed += 1
        if res.get("cover_added"):
            total_covers += 1
        total_lyrics += res.get("lyrics_added", 0)
        if res.get("replaygain_added"):
            total_rg += 1
        total_explicit += res.get("explicit_added", 0)
        if res.get("label"):
            total_labels += 1
        results.append(res)

        if progress_callback:
            try:
                progress_callback(i, total, album_folder.name, res)
            except Exception as e:
                log.warning("Ошибка progress_callback: %s", e)

    return {
        "total": total,
        "processed": processed,
        "covers_added": total_covers,
        "lyrics_added": total_lyrics,
        "replaygain_added": total_rg,
        "explicit_added": total_explicit,
        "labels_added": total_labels,
        "force": force,
        "results": results,
    }
