"""Поиск и скачивание обложек через MusicHoarders API (ADR-0012).

Модуль отправляет HTTP POST на covers.musichoarders.xyz, читает NDJSON-поток
с результатами из нескольких стриминговых сервисов, дедуплицирует кандидатов
по URL полноразмерного изображения и возвращает готовый список.

Скачивание выбранной обложки — отдельная функция: бот вызывает её после того,
как пользователь нажал кнопку в Telegram.
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

try:
    import urllib3.util.connection as urllib_conn
    urllib_conn.HAS_IPV6 = False
except Exception:
    pass

log = logging.getLogger(__name__)

# ── Константы ────────────────────────────────────────────────────────────────

API_URL = "https://covers.musichoarders.xyz/api/search"
YANDEX_API_URL = "https://api.music.yandex.net/search"

# Apple Music исключён: CDN отдаёт WebP под расширением .jpg (ADR-0012).
# yandex опрашивается напрямую через API Яндекс Музыки.
DEFAULT_SOURCES: tuple[str, ...] = (
    "yandex",
    "spotify",
    "deezer",
    "tidal",
    "amazonmusic",
    "discogs",
    "musicbrainz",
)
DEFAULT_COUNTRY = "us"

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:157.0) "
    "Gecko/20100101 Firefox/157.0"
)

# Таймауты (секунды).
# connect — время на установку TCP-соединения.
# stream  — максимальная пауза между NDJSON-строками; если источник завис,
#           через это время iter_lines бросит Timeout, и мы вернём то, что есть.
# download — полное время на скачивание одного изображения.
_CONNECT_TIMEOUT = 10
_STREAM_TIMEOUT = 30
_DOWNLOAD_TIMEOUT = 30


# ── Модель ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class CoverCandidate:
    """Один кандидат обложки, полученный от API.

    Поля ``date`` и ``tracks`` могут отсутствовать — не все источники
    их возвращают.
    """

    small_url: str          # превью ≤480 px — для Telegram MediaGroup
    big_url: str            # полноразмерный оригинал — для вшивания в FLAC
    source: str             # amazonmusic | deezer | spotify | tidal
    title: str              # название релиза из источника
    artist: str             # имя артиста из источника
    date: str | None = None         # "YYYY" или "YYYY-MM-DD"
    tracks: int | None = None       # количество треков (если есть)
    release_url: str | None = None  # ссылка на страницу релиза
    width: int | None = None        # ширина полноразмерной обложки в пикселях
    height: int | None = None       # высота полноразмерной обложки в пикселях

    @property
    def resolution_str(self) -> str:
        """Возвращает строку вида '1425×1425' или пустую строку, если размеры не определены."""
        if self.width and self.height:
            return f"{self.width}×{self.height}"
        return ""


# ── Сходство и ранжирование кандидатов ────────────────────────────────────────

def _norm_str(s: str) -> str:
    """Нормализует строку (только буквы и цифры в нижнем регистре)."""
    return re.sub(r"[^\w]", "", (s or "").lower())


def score_cover_candidate(
    candidate: CoverCandidate,
    target_artist: str,
    target_album: str,
) -> float:
    """Оценивает соответствие кандидата обложки целевому релизу от 0.0 до 1.0.

    Если название релиза в кандидате кардинально отличается от целевого альбома
    (сходство < 0.45), возвращает 0.0 (чужой релиз).
    """
    cand_title = candidate.title or ""
    cand_artist = candidate.artist or ""

    norm_target_alb = _norm_str(target_album)
    norm_cand_alb = _norm_str(cand_title)

    if not norm_target_alb or not norm_cand_alb:
        return 0.0

    # 1. Сходство названия альбома
    if norm_cand_alb == norm_target_alb:
        title_score = 1.0
    elif norm_target_alb in norm_cand_alb or norm_cand_alb in norm_target_alb:
        title_score = 0.85
    else:
        title_score = SequenceMatcher(None, norm_target_alb, norm_cand_alb).ratio()

    # Если название совсем не похоже, это чужой релиз другого альбома того же артиста
    if title_score < 0.45:
        return 0.0

    # 2. Сходство артистов
    target_tokens = [
        _norm_str(t)
        for t in re.split(r"[,;&]|\s+(?:feat\.?|ft\.?|with|x|and)\s+", target_artist, flags=re.I)
        if len(_norm_str(t)) > 1
    ]
    cand_tokens = [
        _norm_str(t)
        for t in re.split(r"[,;&]|\s+(?:feat\.?|ft\.?|with|x|and)\s+", cand_artist, flags=re.I)
        if len(_norm_str(t)) > 1
    ]

    matched_arts = 0
    for tt in target_tokens:
        for ct in cand_tokens:
            if (
                tt == ct
                or (len(tt) > 3 and tt in ct)
                or (len(ct) > 3 and ct in tt)
                or (len(tt) >= 4 and len(ct) >= 4 and SequenceMatcher(None, tt, ct).ratio() >= 0.82)
            ):
                matched_arts += 1
                break

    if target_tokens:
        if matched_arts == 0:
            # Если в кандидате указан сборник (Various Artists / OST), даём невысокий балл
            if any("variou" in ct or "soundtrack" in ct or ct == "va" for ct in cand_tokens):
                art_score = 0.2
            else:
                # Совершенно другой артист (например, чужая песня с тем же названием)
                return 0.0
        else:
            art_score = min(1.0, (matched_arts / len(target_tokens)) + 0.3)
    else:
        art_score = 0.5

    return title_score * 0.7 + art_score * 0.3


def is_valid_cover_candidate(
    candidate: CoverCandidate,
    target_artist: str,
    target_album: str,
    min_score: float = 0.65,
) -> bool:
    """Проверяет, подходит ли кандидат обложки для автоматической установки."""
    return score_cover_candidate(candidate, target_artist, target_album) >= min_score


# ── Поиск Яндекс Музыки ──────────────────────────────────────────────────────

class YandexGeoblockError(Exception):
    """Вызывается при получении HTTP 451 от Яндекс Музыки (геоблокировка)."""


def _build_yandex_cover_url(cover_uri: str, size: str) -> str:
    """Формирует URL обложки Яндекс Музыки заданного размера (напр. 'm1000x1000' или '400x400')."""
    if "%%" in cover_uri:
        url = cover_uri.replace("%%", size)
    else:
        url = f"{cover_uri.rstrip('/')}/{size}"
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"
    return url


def _search_yandex_covers(
    artist: str,
    album: str,
    *,
    token: str | None = None,
) -> list[CoverCandidate]:
    """Ищет обложки релизов напрямую через API Яндекс Музыки.

    Если сервер находится за пределами РФ/СНГ, для обхода ошибки 451
    используется OAuth-токен (параметр YANDEX_MUSIC_TOKEN в .env).
    """
    if not album:
        return []

    if token is None:
        token = os.getenv("YANDEX_MUSIC_TOKEN", "").strip()

    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json",
        "X-Yandex-Music-Client": "YandexMusicAndroid/24023621",
    }
    if token:
        headers["Authorization"] = f"OAuth {token}"

    query = f"{artist} {album}".strip() if artist else album.strip()
    params = {
        "text": query,
        "type": "album",
        "page": 0,
    }

    log.info("Запрос Yandex Music API: '%s'", query)

    try:
        resp = requests.get(
            YANDEX_API_URL,
            headers=headers,
            params=params,
            timeout=(_CONNECT_TIMEOUT, _STREAM_TIMEOUT),
        )
        if resp.status_code == 451:
            log.warning(
                "Yandex Music API вернул 451 (геоблокировка). "
                "Запрос отклонён Яндексом! Укажите YANDEX_MUSIC_TOKEN в .env для работы из-за рубежа."
            )
            raise YandexGeoblockError()
        if resp.status_code == 401:
            log.warning("Yandex Music API: 401 Unauthorized (проверьте YANDEX_MUSIC_TOKEN в .env)")
            return []
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.warning("Ошибка запроса к Yandex Music API: %s", exc)
        return []

    result = data.get("result", {})
    albums_data = result.get("albums", {})
    albums = albums_data.get("results") or albums_data.get("items") or []
    if not albums:
        log.info("Yandex Music: по запросу '%s' ничего не найдено", query)
        return []

    candidates: list[CoverCandidate] = []
    seen_uris: set[str] = set()

    for alb in albums:
        cover_uri = alb.get("coverUri")
        if not cover_uri or cover_uri in seen_uris:
            continue
        seen_uris.add(cover_uri)

        big_url = _build_yandex_cover_url(cover_uri, "m1000x1000")
        small_url = _build_yandex_cover_url(cover_uri, "400x400")

        alb_artists: list[str] = []
        for a in alb.get("artists", []):
            if isinstance(a, dict):
                name = a.get("name", "").strip()
                if name:
                    alb_artists.append(name)
            elif isinstance(a, str) and a.strip():
                alb_artists.append(a.strip())

        cand_artist = ", ".join(alb_artists) if alb_artists else artist
        cand_title = (alb.get("title") or "").strip()
        version = (alb.get("version") or "").strip()
        if version and version.lower() not in cand_title.lower():
            cand_title = f"{cand_title} ({version})"

        rel_date = alb.get("releaseDate")
        year = alb.get("year")
        cand_date: str | None = None
        if rel_date and len(str(rel_date)) >= 10:
            cand_date = str(rel_date)[:10]
        elif year:
            cand_date = str(year)

        alb_id = alb.get("id")
        release_url = f"https://music.yandex.ru/album/{alb_id}" if alb_id else None

        candidate = CoverCandidate(
            small_url=small_url,
            big_url=big_url,
            source="yandex",
            title=cand_title,
            artist=cand_artist,
            date=cand_date,
            tracks=alb.get("trackCount"),
            release_url=release_url,
            width=1000,
            height=1000,
        )
        candidates.append(candidate)
        log.debug("  ← yandex: %s — %s (%s)", candidate.artist, candidate.title, candidate.date or "—")

    log.info("Yandex Music: получено %d кандидатов для '%s'", len(candidates), query)
    return candidates


# ── Поиск MusicHoarders ──────────────────────────────────────────────────────

def _raw_search_covers(
    artist: str,
    album: str,
    *,
    country: str = DEFAULT_COUNTRY,
    sources: tuple[str, ...] = ("amazonmusic", "deezer", "spotify", "tidal"),
) -> list[CoverCandidate]:
    """Выполняет один одиночный HTTP POST запрос к covers.musichoarders.xyz."""
    mh_sources = [s for s in sources if s != "yandex"]
    if not mh_sources:
        return []

    session_id = uuid.uuid4().hex

    headers = {
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/json",
        "Referer": "https://covers.musichoarders.xyz/",
        "Origin": "https://covers.musichoarders.xyz",
        "x-session": session_id,
    }

    payload = {
        "artist": artist,
        "album": album,
        "country": country,
        "sources": mh_sources,
    }

    log.info("Запрос covers API: '%s' — '%s' (источники: %s)", artist, album, ", ".join(mh_sources))

    try:
        resp = requests.post(
            API_URL,
            headers=headers,
            json=payload,
            stream=True,
            timeout=(_CONNECT_TIMEOUT, _STREAM_TIMEOUT),
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Ошибка соединения с covers API: %s", exc)
        return []

    candidates: list[CoverCandidate] = []
    seen_urls: set[str] = set()
    sources_pending = set(mh_sources)

    try:
        for raw_line in resp.iter_lines(decode_unicode=True):
            if not raw_line:
                continue

            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                log.debug("Пропущена невалидная JSON-строка: %s", raw_line[:120])
                continue

            msg_type = obj.get("type")

            if msg_type == "cover":
                big_url = obj.get("bigCoverUrl", "")
                small_url = obj.get("smallCoverUrl", "") or big_url
                if not big_url or big_url in seen_urls:
                    continue

                # Исключаем видео-обложки (Tidal .mp4 и др.), так как они не являются изображениями
                if any(ext in big_url.lower() or ext in small_url.lower() for ext in (".mp4", ".m4v", ".webm", ".mov", "/videos/")):
                    log.debug("  Пропущена видео-обложка: %s", big_url)
                    continue

                seen_urls.add(big_url)
                src = obj.get("source", "unknown")

                # Для Spotify smallCoverUrl генерируется со вторичным хешем b273,
                # который CDN Akamai периодически блокирует или отдаёт 404 серверам Telegram.
                # bigCoverUrl у Spotify всегда отдаётся стабильно.
                if src == "spotify" and "i.scdn.co/image/" in big_url:
                    small_url = big_url

                info = obj.get("releaseInfo", {})
                candidate = CoverCandidate(
                    small_url=small_url,
                    big_url=big_url,
                    source=src,
                    title=info.get("title", ""),
                    artist=info.get("artist", "").strip(),
                    date=info.get("date"),
                    tracks=info.get("tracks"),
                    release_url=info.get("url"),
                )
                candidates.append(candidate)
                log.debug("  ← %s: %s (%s)", candidate.source, candidate.title, candidate.date or "—")

            elif msg_type == "done":
                done_source = obj.get("source", "")
                success = obj.get("success", False)
                sources_pending.discard(done_source)
                log.debug("  ✓ %s done (success=%s), осталось: %d", done_source, success, len(sources_pending))
                if not sources_pending:
                    break

    except requests.exceptions.Timeout:
        remaining = ", ".join(sources_pending) or "—"
        log.warning("Таймаут потока, не дождались: %s. Возвращаем %d кандидатов.", remaining, len(candidates))

    except requests.RequestException as exc:
        log.error("Ошибка чтения потока: %s", exc)

    finally:
        resp.close()

    return candidates


def search_covers(
    artist: str,
    album: str,
    *,
    country: str = DEFAULT_COUNTRY,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
) -> list[CoverCandidate]:
    """Ищет обложки альбома по всем источникам с интеллектуальной фильтрацией и ранжированием.

    - Поддерживает мультиартист релизы (пробует полный состав и фолбэки по ключевым соавторам).
    - Оценивает релевантность кандидатов названию альбома и артистам.
    - Отсекает нерелевантные релизы (счет 0.0), чтобы чужие альбомы не попали в результат.
    - Упорядочивает кандидатов Round-Robin среди наиболее релевантных совпадений.
    """
    if not artist or not album:
        return []

    # Формируем список поисковых запросов для артиста
    tokens = [
        t.strip()
        for t in re.split(r"[,;&]|\s+(?:feat\.?|ft\.?|with|x|and)\s+", artist, flags=re.I)
        if t.strip()
    ]
    queries = [artist]
    if len(tokens) >= 2:
        for i in range(len(tokens)):
            for j in range(i + 1, len(tokens)):
                p = f"{tokens[i]} & {tokens[j]}"
                if p not in queries:
                    queries.append(p)
                p_comma = f"{tokens[i]}, {tokens[j]}"
                if p_comma not in queries:
                    queries.append(p_comma)
    for tok in tokens:
        if tok not in queries:
            queries.append(tok)

    all_candidates: list[CoverCandidate] = []
    seen_urls: set[str] = set()

    # 1. Поиск через API Яндекс Музыки
    if "yandex" in sources:
        yandex_queries = list(queries)
        clean_art = re.sub(r"&", " ", artist).strip()
        if clean_art not in yandex_queries:
            yandex_queries.insert(1, clean_art)

        found_good_yandex = False
        try:
            for y_artist in yandex_queries:
                yandex_raw = _search_yandex_covers(y_artist, album)
                for c in yandex_raw:
                    if not c.big_url or c.big_url in seen_urls:
                        continue
                    seen_urls.add(c.big_url)
                    score = score_cover_candidate(c, artist, album)
                    if score > 0.4:
                        all_candidates.append(c)
                        log.info("  ✓ yandex: %s — %s (балл: %.2f)", c.artist, c.title, score)
                        if score >= 0.7:
                            found_good_yandex = True
                    else:
                        log.debug("  ✗ yandex (отклонён, балл %.2f): %s — %s", score, c.artist, c.title)

                if found_good_yandex:
                    break

            # Если поиск по артисту не дал результатов, пробуем прямой поиск по названию альбома
            if not found_good_yandex:
                yandex_raw = _search_yandex_covers("", album)
                for c in yandex_raw:
                    if not c.big_url or c.big_url in seen_urls:
                        continue
                    seen_urls.add(c.big_url)
                    score = score_cover_candidate(c, artist, album)
                    if score > 0.4:
                        all_candidates.append(c)
                        log.info("  ✓ yandex (по названию альбома): %s — %s (балл: %.2f)", c.artist, c.title, score)
        except YandexGeoblockError:
            pass

    # 2. Поиск через MusicHoarders (Deezer, Spotify, Tidal, Amazon, Discogs, MusicBrainz)
    mh_sources = tuple(s for s in sources if s != "yandex")
    if mh_sources:
        total_high_matches = 0
        for q_idx, q_artist in enumerate(queries):
            raw_cands = _raw_search_covers(q_artist, album, country=country, sources=mh_sources)
            new_high_matches = 0
            for c in raw_cands:
                if not c.big_url or c.big_url in seen_urls:
                    continue
                seen_urls.add(c.big_url)
                score = score_cover_candidate(c, artist, album)
                if score > 0.4:
                    all_candidates.append(c)
                    if score >= 0.7:
                        new_high_matches += 1
                        total_high_matches += 1

            # Если уже на первом или текущем запросе нашли как минимум 2 отличных совпадения
            # (или 1 с высоким баллом для соло-артиста), дальнейшие запросы не нужны
            if new_high_matches >= 2 or (q_idx == 0 and new_high_matches >= 1 and len(tokens) <= 1):
                break

        # Если поиск по артистам не дал ни одного высокого совпадения,
        # пробуем запрос чисто по названию альбома (для уникальных названий вроде "Умшакалака")
        if total_high_matches == 0:
            raw_cands = _raw_search_covers("", album, country=country, sources=mh_sources)
            for c in raw_cands:
                if not c.big_url or c.big_url in seen_urls:
                    continue
                seen_urls.add(c.big_url)
                score = score_cover_candidate(c, artist, album)
                if score > 0.4:
                    all_candidates.append(c)

    if not all_candidates:
        log.warning("Поиск обложек: для '%s — %s' не найдено релевантных обложек", artist, album)
        return []

    # Обогащаем кандидатов информацией о разрешении полноразмерных обложек
    all_candidates = enrich_candidates_dimensions(all_candidates)

    # Ранжируем кандидатов по баллам и группируем по тирам релевантности
    scored_candidates = [(score_cover_candidate(c, artist, album), c) for c in all_candidates]
    scored_candidates.sort(key=lambda x: x[0], reverse=True)

    high_tier = [c for s, c in scored_candidates if s >= 0.65]
    med_tier = [c for s, c in scored_candidates if 0.4 <= s < 0.65]

    ordered_high = reorder_candidates_round_robin(high_tier, sources)
    ordered_med = reorder_candidates_round_robin(med_tier, sources)

    result = ordered_high + ordered_med
    log.info(
        "Найдено %d релевантных кандидатов для '%s — %s' (топ: %d)",
        len(result), artist, album, len(high_tier)
    )
    for i, c in enumerate(result[:6]):
        log.info("  #%d [%s] %s — %s", i + 1, c.source, c.resolution_str or "—", c.title)
    return result


# ── Определение разрешения обложек ──────────────────────────────────────────

def parse_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Извлекает размеры (width, height) из первых байт заголовка изображения.

    Поддерживает форматы JPEG, PNG, WebP и GIF без внешних зависимостей.
    """
    if not data:
        return None

    # PNG
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])

    # GIF
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])

    # JPEG
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xff:
                i += 1
                continue
            marker = data[i + 1]
            # Маркеры Start of Frame (SOF0 - SOF15, исключая DHT 0xc4, JPG 0xc8, DAC 0xcc)
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return (w, h)
            if marker in (0xd9, 0xda):  # EOI или SOS
                break
            if i + 4 > len(data):
                break
            length = struct.unpack(">H", data[i + 2 : i + 4])[0]
            if length < 2:
                break
            i += 2 + length

    # WebP
    if data.startswith(b"RIFF") and len(data) >= 30 and data[8:12] == b"WEBP":
        vp8_type = data[12:16]
        if vp8_type == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return (w & 0x3fff, h & 0x3fff)
        elif vp8_type == b"VP8L":
            b0, b1, b2, b3 = data[21:25]
            w = 1 + (((b1 & 0x3f) << 8) | b0)
            h = 1 + (((b3 & 0xf) << 10) | (b2 << 2) | ((b1 & 0xc0) >> 6))
            return (w, h)
        elif vp8_type == b"VP8X":
            w = 1 + struct.unpack("<I", data[24:27] + b"\x00")[0]
            h = 1 + struct.unpack("<I", data[27:30] + b"\x00")[0]
            return (w, h)

    return None


def infer_dimensions_from_url(url: str, source: str) -> tuple[int, int] | None:
    """Мгновенно извлекает разрешение из структуры URL источника без сетевых запросов."""
    if not url:
        return None
    if source == "yandex" or "1000x1000" in url:
        return (1000, 1000)
    m_dz = re.search(r"/(\d+)x0-", url)
    if m_dz:
        dim = int(m_dz.group(1))
        return (dim, dim)
    w = re.search(r"/w:(\d+)", url)
    h = re.search(r"/h:(\d+)", url)
    if w and h:
        return (int(w.group(1)), int(h.group(1)))
    m_tid = re.search(r"/(\d+)x(\d+)\.", url)
    if m_tid:
        return (int(m_tid.group(1)), int(m_tid.group(2)))
    if source == "spotify":
        if "000082c1" in url:
            return (1425, 1425)
        elif "0000b273" in url:
            return (640, 640)
    return None


def probe_image_dimensions(url: str, timeout: float = 2.0) -> tuple[int, int] | None:
    """Определяет разрешение изображения, читая первые несколько килобайт потока."""
    if not url:
        return None
    headers = {
        "User-Agent": _BROWSER_UA,
        "Range": "bytes=0-65535",
        "Accept": "image/*,*/*;q=0.8",
    }
    try:
        with requests.get(url, stream=True, timeout=timeout, headers=headers) as r:
            if r.status_code not in (200, 206):
                return None
            chunk = b""
            for c in r.iter_content(chunk_size=16384):
                chunk += c
                dim = parse_image_dimensions(chunk)
                if dim:
                    return dim
                if len(chunk) > 131072:
                    break
    except Exception:
        pass
    return None


DEFAULT_SOURCE_DIMENSIONS: dict[str, tuple[int, int]] = {
    "tidal": (1425, 1425),
    "amazonmusic": (1425, 1425),
    "spotify": (1425, 1425),
    "deezer": (1400, 1400),
    "yandex": (1000, 1000),
    "musicbrainz": (1200, 1200),
    "discogs": (600, 600),
}


def enrich_candidates_dimensions(
    candidates: list[CoverCandidate],
) -> list[CoverCandidate]:
    """Обогащает список кандидатов информацией о разрешении оригинала (width, height).

    Использует многопоточную проверку заголовков с таймаутом и фолбэк на эвристики URL.
    """
    if not candidates:
        return []

    resolved: dict[str, tuple[int, int]] = {}
    urls_to_probe: set[str] = set()

    for c in candidates:
        if c.width and c.height:
            resolved[c.big_url] = (c.width, c.height)
            continue
        inferred = infer_dimensions_from_url(c.big_url, c.source)
        if inferred:
            resolved[c.big_url] = inferred
        else:
            urls_to_probe.add(c.big_url)

    if urls_to_probe:
        probe_list = list(urls_to_probe)
        with ThreadPoolExecutor(max_workers=min(16, len(probe_list))) as pool:
            probe_results = dict(zip(probe_list, pool.map(probe_image_dimensions, probe_list)))
        for u, dim in probe_results.items():
            if dim:
                resolved[u] = dim

    enriched: list[CoverCandidate] = []
    for c in candidates:
        dim = resolved.get(c.big_url) or DEFAULT_SOURCE_DIMENSIONS.get(c.source, (0, 0))
        if dim and dim != (0, 0) and (not c.width or not c.height):
            enriched.append(replace(c, width=dim[0], height=dim[1]))
        else:
            enriched.append(c)

    return enriched


def _candidate_quality_key(c: CoverCandidate) -> tuple[int, int, int]:
    """Ключ для ранжирования кандидатов по качеству (разрешению).

    1. Общая площадь в пикселях (w * h)
    2. Минимальная сторона (гарантия отсутствия вытянутых баннеров)
    3. Вес источника при одинаковом разрешении
    """
    w = c.width or 0
    h = c.height or 0
    area = w * h
    min_side = min(w, h)
    source_weights = {
        "tidal": 60,
        "amazonmusic": 55,
        "deezer": 50,
        "spotify": 40,
        "yandex": 30,
        "musicbrainz": 20,
        "discogs": 10,
    }
    src_weight = source_weights.get(c.source, 0)
    return (area, min_side, src_weight)


def reorder_candidates_round_robin(
    candidates: list[CoverCandidate],
    sources: tuple[str, ...],
) -> list[CoverCandidate]:
    """Упорядочивает кандидатов методом Round-Robin по источникам с сортировкой по качеству.

    В каждом батче (раунде) берётся по одному кандидату от каждого доступного источника,
    а между собой кандидаты внутри батча отсортированы по убыванию качества (разрешения).
    """
    by_source: dict[str, list[CoverCandidate]] = {s: [] for s in sources}
    other: list[CoverCandidate] = []

    for c in candidates:
        if c.source in by_source:
            by_source[c.source].append(c)
        else:
            other.append(c)

    # Внутри каждого источника предварительно сортируем варианты по убыванию качества
    for s in by_source:
        by_source[s].sort(key=_candidate_quality_key, reverse=True)

    ordered: list[CoverCandidate] = []
    max_len = max([len(v) for v in by_source.values()] + [0])

    for round_idx in range(max_len):
        round_cands = [by_source[s][round_idx] for s in sources if round_idx < len(by_source[s])]
        # Сортируем кандидатов текущего раунда (батча) между собой по убыванию качества
        round_cands.sort(key=_candidate_quality_key, reverse=True)
        ordered.extend(round_cands)

    if other:
        other.sort(key=_candidate_quality_key, reverse=True)
        ordered.extend(other)

    return ordered


# ── Скачивание ───────────────────────────────────────────────────────────────

def download_cover(url: str, dest: Path) -> Path:
    """Скачивает полноразмерную обложку и сохраняет на диск.

    Args:
        url: Адрес ``bigCoverUrl`` выбранного кандидата.
        dest: Путь для сохранения (обычно ``album_folder / "cover.jpg"``).

    Returns:
        Путь к сохранённому файлу (== *dest*).

    Raises:
        requests.RequestException: Сетевая ошибка при скачивании.
        ValueError: Сервер вернул пустое тело ответа.
    """
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "image/*,*/*;q=0.8",
    }

    log.info("Скачивание обложки: %s", url)

    resp = requests.get(url, headers=headers, timeout=_DOWNLOAD_TIMEOUT)
    resp.raise_for_status()

    if not resp.content:
        raise ValueError(f"Пустой ответ при скачивании обложки: {url}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)

    size_kb = len(resp.content) // 1024
    log.info("Обложка сохранена: %s (%d KB)", dest.name, size_kb)

    return dest
