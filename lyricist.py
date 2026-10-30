"""Поиск, загрузка и управление текстами песен (.lrc / plain text).

Каскадный водопад поиска:
  1. LRCLIB (synced) — синхронизированный караоке-текст с таймкодами.
  2. LRCLIB (plain)  — обычный текст без таймкодов, если караоке не найдено.
  3. Genius (plain)  — эталонный текст с Genius.com как надёжный универсальный фоллбэк.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from mutagen.flac import FLAC

log = logging.getLogger(__name__)


# ── Модель ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class LyricResult:
    """Результат поиска текста песни."""

    text: str                # Содержимое (LRC с таймкодами или обычный текст)
    is_synced: bool          # True, если текст синхронизированный (караоке)
    source: str              # "lrclib" | "genius" | "manual"
    track_title: str = ""
    artist: str = ""


# ── Вспомогательные функции ──────────────────────────────────────────────────

def has_timestamps(text: str) -> bool:
    """Проверяет, содержит ли текст временные метки караоке вида [mm:ss.xx]."""
    return bool(re.search(r"\[\d{2}:\d{2}", text))


def preview_lyrics(text: str, max_lines: int = 6) -> str:
    """Возвращает компактное превью первых N строк текста."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    preview = lines[:max_lines]
    if len(lines) > max_lines:
        preview.append("…")
    return "\n".join(preview)


# Паттерн для китайских метаданных из KuGou/NetEase (作词, 作曲, 编曲 и т.д.)
CJK_META_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}(?:\.\d+)?\]\s*)?(?:(?:作\s*词|作\s*曲|编\s*曲|制\s*作\s*人|录\s*音|混\s*音|母\s*带|监\s*制|和\s*音|企\s*划|出\s*品|发\s*行|词|曲|原\s*唱|翻\s*唱|演\s*唱)\s*[:：\s/\-]|(?:未经.*?许可|不得.*?使用|网易云音乐|QQ音乐|酷狗音乐))",
    re.I,
)


def extract_featured_artists(title: str) -> list[str]:
    """Извлекает имена приглашённых артистов из скобок feat/ft/with в названии трека."""
    if not title:
        return []
    m = re.search(r"(?i)[\(\[](?:feat\.?|ft\.?|with)\s+([^()\[\]]+)[\)\]]", title)
    if not m:
        m = re.search(r"(?i)\s+(?:feat\.?|ft\.?)\s+(.+)$", title)
    if not m:
        return []
    raw = m.group(1).strip()
    return [a.strip() for a in re.split(r"[,;&]|\s+(?:and|и|x)\s+", raw, flags=re.I) if a.strip()]


def clean_track_title(t: str) -> str:
    """Удаляет номера треков, префиксы, feat/ft/with/remastered/explicit/etc."""
    t = re.sub(r"^\d+[\s\.\-_]+", "", t or "")
    t = re.sub(
        r"(?i)\s*[\(\[](?:feat\.?|ft\.?|with|prod\.?|official\s+video|explicit|remastered|bonus\s+track).*?[\)\]]",
        "",
        t,
    )
    if " - " in t:
        t = t.split(" - ")[-1]
    return t.strip()


def is_translation(title: str, artist: str) -> bool:
    """Проверяет, является ли кандидат переводом или кавером на другой язык."""
    bad = (
        "translation", "traducción", "traduccion", "tradução", "traducao",
        "çeviri", "ceviri", "romanized", "übersetzung", "traduzione",
        "paroles", "deutsche"
    )
    combined = f"{title} {artist}".lower()
    return any(b in combined for b in bad)


def clean_lrc_metadata(text: str) -> str:
    """Удаляет мусорные служебные строки (китайские кредиты 作词/作曲 из KuGou/NetEase)."""
    if not text:
        return ""
    lines = []
    for l in text.splitlines():
        trimmed = l.strip()
        if not trimmed or CJK_META_RE.match(trimmed):
            continue
        lines.append(trimmed)
    return "\n".join(lines).strip()


# ── Менеджер текстов ──────────────────────────────────────────────────────────

class LyricManager:
    """Поиск, загрузка и форматирование текстов песен."""

    BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    LRCLIB_UA = BROWSER_UA

    # ── 1. LRCLIB ─────────────────────────────────────────────────────────────

    def _fetch_lrclib(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int = 0,
    ) -> tuple[LyricResult | None, LyricResult | None]:
        """Ищет текст в LRCLIB через точный и мягкий поиск.

        Returns:
            Кортеж (synced_result, plain_result).
        """
        headers = {"User-Agent": self.BROWSER_UA}

        clean_t = clean_track_title(title)
        title_feats = extract_featured_artists(title)
        artist_tokens = [a.strip() for a in re.split(r"[,;&]|\s+(?:feat\.?|ft\.?|x|and)\s+", artist, flags=re.I) if a.strip()]
        for f_art in title_feats:
            if not any(f_art.lower() == a.lower() for a in artist_tokens):
                artist_tokens.append(f_art)

        first_art = artist_tokens[0] if artist_tokens else artist

        log.info("Поиск текстов [LRCLIB]: '%s — %s'", artist, title)

        # Шаг 1: Точный поиск по метаданным (пробуем полный артист и первого артиста)
        exact_trials = [
            {"artist_name": artist, "track_name": title, "album_name": album, "duration": duration},
            {"artist_name": first_art, "track_name": clean_t, "album_name": album},
        ]
        if clean_t != title:
            exact_trials.insert(1, {"artist_name": artist, "track_name": clean_t, "album_name": album})
        if len(artist_tokens) >= 2:
            exact_trials.insert(1, {"artist_name": first_art, "track_name": f"{clean_t} (feat. {artist_tokens[1]})", "album_name": album})

        for exact_params in exact_trials:
            params = {k: v for k, v in exact_params.items() if v}
            try:
                r = requests.get(
                    "https://lrclib.net/api/get",
                    params=params,
                    headers=headers,
                    timeout=6,
                )
                if r.status_code == 200:
                    data = r.json()
                    synced = clean_lrc_metadata(data.get("syncedLyrics"))
                    plain = clean_lrc_metadata(data.get("plainLyrics"))
                    res_synced = (
                        LyricResult(text=synced, is_synced=True, source="lrclib", track_title=title, artist=artist)
                        if synced
                        else None
                    )
                    res_plain = (
                        LyricResult(text=plain, is_synced=False, source="lrclib", track_title=title, artist=artist)
                        if plain
                        else None
                    )
                    if res_synced or res_plain:
                        types = []
                        if res_synced:
                            types.append("караоке")
                        if res_plain:
                            types.append("plain")
                        log.info("  ✓ LRCLIB: точное совпадение получено (%s)", " + ".join(types))
                        return res_synced, res_plain
            except Exception as e:
                log.debug("LRCLIB exact match error: %s", e)

        # Шаг 2: Мягкий поиск по ключевым словам
        target_base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", clean_t).strip()
        expanded_queries = []
        # Если есть приглашённые артисты — СНАЧАЛА ищем полный состав (с фитами), чтобы не скачать урезанную версию!
        if len(artist_tokens) >= 2:
            expanded_queries.append(f"{artist_tokens[0]} {artist_tokens[1]} {clean_t}")
            expanded_queries.append(f"{artist} {clean_t}")
            expanded_queries.append(f"{artist_tokens[0]} feat. {artist_tokens[1]} {clean_t}")
        if first_art:
            q_first = f"{first_art} {clean_t}"
            if q_first not in expanded_queries:
                expanded_queries.append(q_first)
        if clean_t != title and f"{first_art} {title}" not in expanded_queries:
            expanded_queries.append(f"{first_art} {title}")
        if target_base and target_base != clean_t:
            if first_art:
                q_base = f"{first_art} {target_base}"
                if q_base not in expanded_queries:
                    expanded_queries.append(q_base)
            if f"{artist} {target_base}" not in expanded_queries:
                expanded_queries.append(f"{artist} {target_base}")
        for tok in artist_tokens:
            spaced = re.sub(r"\.([A-Za-zА-Яа-я])", r". \1", tok)
            for t_var in (tok, spaced):
                q_var = f"{t_var} {clean_t}"
                if q_var not in expanded_queries:
                    expanded_queries.append(q_var)
                if target_base and target_base != clean_t:
                    q_var_base = f"{t_var} {target_base}"
                    if q_var_base not in expanded_queries:
                        expanded_queries.append(q_var_base)

        best_plain = None
        for q in expanded_queries:
            try:
                log.info("  Запрос LRCLIB: '%s'", q)
                r = requests.get(
                    "https://lrclib.net/api/search",
                    params={"q": q},
                    headers=headers,
                    timeout=8,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        log.info("  LRCLIB: получено записей: %d", len(data))
                        target_clean = clean_track_title(title).lower()
                        target_base_clean = clean_track_title(target_base).lower()

                        cand_synced_list = []
                        cand_plain_list = []

                        for item in data:
                            api_title = item.get("trackName", "")
                            api_art = item.get("artistName", "")
                            api_title_clean = clean_track_title(api_title).lower()
                            api_base_clean = clean_track_title(re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", api_title)).lower()

                            # Сверка названий трека
                            matched_title = (
                                target_clean == api_title_clean
                                or (target_base_clean and api_base_clean and target_base_clean == api_base_clean)
                                or (target_clean in api_title_clean if len(target_clean) >= 4 else False)
                                or (api_title_clean in target_clean if len(api_title_clean) >= 4 else False)
                                or SequenceMatcher(None, target_clean, api_title_clean).ratio() >= 0.85
                                or (
                                    target_base_clean
                                    and api_base_clean
                                    and SequenceMatcher(None, target_base_clean, api_base_clean).ratio() >= 0.85
                                )
                            )
                            if not matched_title:
                                continue

                            synced = clean_lrc_metadata(item.get("syncedLyrics"))
                            plain = clean_lrc_metadata(item.get("plainLyrics"))

                            # Оценка наличия фитов в кандидате
                            has_feat = False
                            if len(artist_tokens) >= 2:
                                cand_all = (api_art + " " + api_title).lower()
                                for f_art in artist_tokens[1:]:
                                    if f_art.lower() in cand_all:
                                        has_feat = True
                                        break

                            if synced:
                                cand_synced_list.append((has_feat, synced, plain, api_title))
                            if plain:
                                cand_plain_list.append((has_feat, plain, api_title))

                        # Если искали трек с фитом, приоритет отдаём кандидатам с фитом
                        if len(artist_tokens) >= 2:
                            feat_synced = [c for c in cand_synced_list if c[0]]
                            if feat_synced:
                                _, s_txt, p_txt, a_t = feat_synced[0]
                                log.info("  ✓ LRCLIB: выбрано караоке с фитом для '%s'", a_t)
                                return (
                                    LyricResult(text=s_txt, is_synced=True, source="lrclib", track_title=title, artist=artist),
                                    LyricResult(text=p_txt, is_synced=False, source="lrclib", track_title=title, artist=artist) if p_txt else None,
                                )
                            feat_plain = [c for c in cand_plain_list if c[0]]
                            if feat_plain and not best_plain:
                                _, p_txt, a_t = feat_plain[0]
                                log.info("  ✓ LRCLIB: найден текст с фитом для '%s'", a_t)
                                best_plain = LyricResult(text=p_txt, is_synced=False, source="lrclib", track_title=title, artist=artist)

                        if cand_synced_list:
                            _, s_txt, p_txt, a_t = cand_synced_list[0]
                            log.info("  ✓ LRCLIB: выбрано караоке для '%s'", a_t)
                            return (
                                LyricResult(text=s_txt, is_synced=True, source="lrclib", track_title=title, artist=artist),
                                LyricResult(text=p_txt, is_synced=False, source="lrclib", track_title=title, artist=artist) if p_txt else None,
                            )
                        if cand_plain_list and not best_plain:
                            _, p_txt, a_t = cand_plain_list[0]
                            log.info("  ✓ LRCLIB: найден обычный текст для '%s'", a_t)
                            best_plain = LyricResult(text=p_txt, is_synced=False, source="lrclib", track_title=title, artist=artist)

                        if best_plain:
                            return None, best_plain
            except Exception as e:
                log.debug("LRCLIB search error: %s", e)

        if not best_plain:
            log.info("  LRCLIB: подходящих текстов не найдено")

        return None, None

    # ── 2. Genius ─────────────────────────────────────────────────────────────

    def _fetch_genius(self, artist: str, title: str) -> LyricResult | None:
        """Ищет эталонный текст песни на Genius.com с ранжированием релевантности кандидатов."""
        headers = {"User-Agent": self.BROWSER_UA}

        log.info("Поиск текстов [Genius]: '%s — %s'", artist, title)

        def norm(s: str) -> str:
            return re.sub(r"[^\w]", "", (s or "").lower())

        clean_t = clean_track_title(title)
        title_feats = extract_featured_artists(title)
        artist_tokens = [a.strip() for a in re.split(r"[,;&]|\s+(?:feat\.?|ft\.?|x|and)\s+", artist, flags=re.I) if a.strip()]
        for f_art in title_feats:
            if not any(f_art.lower() == a.lower() for a in artist_tokens):
                artist_tokens.append(f_art)

        expanded_tokens = []
        for a in artist_tokens:
            if a not in expanded_tokens:
                expanded_tokens.append(a)
            spaced = re.sub(r"\.([A-Za-zА-Яа-я])", r". \1", a)
            if spaced != a and spaced not in expanded_tokens:
                expanded_tokens.append(spaced)

        target_title_norm = norm(clean_t)
        target_base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", clean_t).strip()
        target_base_norm = norm(target_base)
        clean_base = re.sub(r"[^\w\s]", " ", target_base)
        clean_base = re.sub(r"\s+", " ", clean_base).strip()
        clean_base_norm = norm(clean_base)
        clean_target_arts = [norm(a) for a in expanded_tokens if len(norm(a)) > 1]

        queries: list[str] = []

        def add_query(q: str):
            q = (q or "").strip()
            if q and q not in queries:
                queries.append(q)

        # Сначала ищем по полному составу с фитами, чтобы получить полную версию текста
        if len(expanded_tokens) >= 2:
            add_query(f"{expanded_tokens[0]} {expanded_tokens[1]} {clean_t}")
            add_query(f"{artist} {clean_t}")
            add_query(f"{expanded_tokens[0]} feat. {expanded_tokens[1]} {clean_t}")
        if expanded_tokens:
            add_query(f"{expanded_tokens[0]} {clean_t}")
        add_query(f"{artist} {clean_t}")
        if clean_t != title and expanded_tokens:
            add_query(f"{expanded_tokens[0]} {title}")

        # Поиск по базовому названию без скобок (например, без каверзных каомодзи/feat)
        if target_base and target_base != clean_t:
            if expanded_tokens:
                add_query(f"{expanded_tokens[0]} {target_base}")
            add_query(f"{artist} {target_base}")

        # Поиск по очищенному от любых спецсимволов базовому названию
        if clean_base and clean_base not in (clean_t, target_base):
            if expanded_tokens:
                add_query(f"{expanded_tokens[0]} {clean_base}")
            add_query(f"{artist} {clean_base}")

        for tok in expanded_tokens:
            add_query(f"{tok} {clean_t}")
            if target_base and target_base != clean_t:
                add_query(f"{tok} {target_base}")
            if clean_base and clean_base not in (clean_t, target_base):
                add_query(f"{tok} {clean_base}")

        all_hits: list[dict] = []
        seen_urls = set()

        for q in queries:
            try:
                log.info("  Запрос Genius API: '%s'", q)
                r = requests.get(
                    "https://genius.com/api/search/multi",
                    params={"q": q},
                    headers=headers,
                    timeout=8,
                )
                if r.status_code == 200:
                    sections = r.json().get("response", {}).get("sections", [])
                    found_exact_song = False
                    new_hits_count = 0
                    for s in sections:
                        for h in s.get("hits", []):
                            if h.get("type") == "song" or s.get("type") == "song":
                                res = h.get("result", {})
                                url = res.get("url")
                                if url and url not in seen_urls:
                                    seen_urls.add(url)
                                    h_title = res.get("title", "")
                                    h_art = res.get("artist_names", "") + " " + res.get("primary_artist", {}).get("name", "")
                                    if not is_translation(h_title, h_art):
                                        all_hits.append(res)
                                        new_hits_count += 1
                                        h_clean = clean_track_title(h_title)
                                        h_base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", h_clean).strip()
                                        h_clean_base = re.sub(r"[^\w\s]", " ", h_base)
                                        h_clean_base = re.sub(r"\s+", " ", h_clean_base).strip()
                                        if (
                                            norm(h_clean) == target_title_norm
                                            or (h_base and norm(h_base) == target_title_norm)
                                            or (target_base_norm and norm(h_clean) == target_base_norm)
                                            or (h_base and target_base_norm and norm(h_base) == target_base_norm)
                                            or (clean_base_norm and norm(h_clean_base) == clean_base_norm)
                                        ):
                                            found_exact_song = True
                    log.info("  Genius API: получено кандидатов: %d (новых: %d)", len(all_hits), new_hits_count)
                    if found_exact_song and len(all_hits) >= 2:
                        break
            except Exception as e:
                log.debug("Genius search query '%s' error: %s", q, e)

        if not all_hits:
            log.info("  Genius: кандидатов не найдено")
            return None

        # Ранжирование кандидатов
        best_hit = None
        best_score = -1

        for res in all_hits:
            hit_title = res.get("title", "")
            art_names = (res.get("artist_names", "") + " " + res.get("primary_artist", {}).get("name", "")).lower()

            if is_translation(hit_title, art_names):
                continue
            if "remix" in hit_title.lower() and "remix" not in title.lower():
                continue

            hit_title_clean = clean_track_title(hit_title)
            hit_title_norm = norm(hit_title_clean)
            if not hit_title_norm:
                continue

            hit_base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", hit_title_clean).strip()
            hit_base_norm = norm(hit_base)
            hit_clean_base = re.sub(r"[^\w\s]", " ", hit_base)
            hit_clean_base = re.sub(r"\s+", " ", hit_clean_base).strip()
            hit_clean_base_norm = norm(hit_clean_base)
            hit_parens = [norm(p) for p in re.findall(r"[\(\[](.*?)[\)\]]", hit_title_clean)]

            score = 0
            # 1. Совпадение названия
            if hit_title_norm == target_title_norm:
                score += 60
                if hit_title_clean.strip().lower() == clean_t.strip().lower():
                    score += 20
            elif hit_base_norm == target_title_norm or (target_base_norm and hit_base_norm == target_base_norm):
                score += 55
            elif target_base_norm and hit_title_norm == target_base_norm:
                score += 55
            elif clean_base_norm and (
                hit_clean_base_norm == clean_base_norm
                or hit_base_norm == clean_base_norm
                or hit_title_norm == clean_base_norm
            ):
                score += 55
            elif target_title_norm in hit_parens or (target_base_norm and target_base_norm in hit_parens):
                score += 50
            else:
                sim = max(
                    SequenceMatcher(None, target_title_norm, hit_title_norm).ratio(),
                    SequenceMatcher(None, target_title_norm, hit_base_norm).ratio() if hit_base_norm else 0,
                    SequenceMatcher(None, target_base_norm, hit_base_norm).ratio() if (target_base_norm and hit_base_norm) else 0,
                    SequenceMatcher(None, clean_base_norm, hit_clean_base_norm).ratio() if (clean_base_norm and hit_clean_base_norm) else 0,
                )
                if sim >= 0.8:
                    score += int(sim * 50)
                else:
                    continue  # Название слишком отличается

            # 2. Совпадение артистов (чем больше артистов релиза совпало, тем выше балл)
            cand_art_norm = norm(art_names)
            matched_arts = sum(1 for a in clean_target_arts if a in cand_art_norm)
            if not matched_arts:
                continue

            score += matched_arts * 30

            # Бонус за полное совпадение всех соавторов / фитов и защита от версии без фитов
            if len(clean_target_arts) >= 2:
                if matched_arts == len(clean_target_arts):
                    score += 40
                elif matched_arts == 1:
                    score -= 20

            if score > best_score:
                best_score = score
                best_hit = res

        if not best_hit:
            log.info("  Genius: подходящих кандидатов по порогу схожести не найдено")
            return None

        song_url = best_hit.get("url")
        best_art_name = best_hit.get("artist_names", "") or best_hit.get("primary_artist", {}).get("name", "")
        log.info("  ✓ Genius: выбран кандидат '%s — %s' (балл: %d)", best_art_name, best_hit.get("title", ""), best_score)
        if not song_url:
            return None

        try:
            log.info("  Загрузка текста Genius: %s", song_url)
            page = requests.get(song_url, headers=headers, timeout=10)
            if page.status_code != 200:
                log.warning("  Genius: ошибка загрузки страницы HTTP %d", page.status_code)
                return None

            soup = BeautifulSoup(page.text, "html.parser")

            # Удаляем служебные шапки контрибьюторов и сайдбары Genius
            for bad in soup.find_all(class_=lambda cl: cl and any(x in cl for x in ["LyricsHeader", "LyricsEditDesktop", "Sidebar"])):
                bad.decompose()

            for br in soup.find_all("br"):
                br.replace_with("\n")

            containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
            if not containers:
                log.warning("  Genius: контейнеры с текстом не найдены на странице")
                return None

            full_text = "\n".join(c.get_text() for c in containers)
            full_text = re.sub(r"\n{3,}", "\n\n", full_text)
            full_text = re.sub(r"\d*Embed$", "", full_text.strip())

            full_text = clean_lrc_metadata(full_text.strip())

            if full_text:
                log.info("  ✓ Genius: успешно загружен текст песни (%d строк)", len(full_text.splitlines()))
                return LyricResult(
                    text=full_text,
                    is_synced=False,
                    source="genius",
                    track_title=title,
                    artist=artist,
                )
        except Exception as e:
            log.debug("Genius fetch error: %s", e)

        return None

    # ── 3. LRCMUX (KuGou, Musixmatch, NetEase, YouTube Music) ─────────────────

    def _fetch_lrcmux(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int = 0,
    ) -> LyricResult | None:
        """Ищет синхронизированный текст через мультиплексор lrcmux (KuGou, Musixmatch, NetEase, YTMusic)."""
        artist_clean = artist.strip()
        title_clean = title.strip()
        if not artist_clean or not title_clean:
            return None

        log.info("Поиск караоке [lrcmux]: '%s — %s'", artist_clean, title_clean)

        clean_t = clean_track_title(title_clean)
        target_norm = re.sub(r"[^\w]", "", clean_t.lower())
        target_base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", clean_t).strip()
        target_base_norm = re.sub(r"[^\w]", "", target_base.lower())

        queries = [title_clean]
        if clean_t and clean_t != title_clean:
            queries.append(clean_t)
        if target_base and target_base not in queries:
            queries.append(target_base)

        for q_title in queries:
            params = {"artist": artist_clean, "title": q_title}
            if album.strip():
                params["album"] = album.strip()
            if duration > 0:
                params["duration"] = str(duration)

            log.info("  Запрос lrcmux API: '%s — %s'", artist_clean, q_title)
            try:
                r = requests.get(
                    "https://api.lrcmux.dev/get",
                    params=params,
                    headers={"User-Agent": self.BROWSER_UA},
                    timeout=6,
                )
                if r.status_code == 200:
                    data = r.json()
                    # Валидация заголовка ответа API (если сервис вернул title)
                    api_title = data.get("title", "")
                    if api_title:
                        c_clean = clean_track_title(api_title).lower()
                        c_norm = re.sub(r"[^\w]", "", c_clean)
                        if (
                            c_norm != target_norm
                            and (not target_base_norm or c_norm != target_base_norm)
                            and SequenceMatcher(None, target_norm, c_norm).ratio() < 0.7
                        ):
                            log.warning("  ⚠️ lrcmux вернул трек '%s' вместо '%s' — отклонено", api_title, title)
                            continue

                    lines = data.get("lines", [])
                    if lines:
                        out = []
                        has_sync = False
                        for line in lines:
                            txt = line.get("text", "").strip()
                            start_ms = line.get("start")
                            if start_ms is not None:
                                has_sync = True
                                total_sec = max(0.0, start_ms / 1000.0)
                                m = int(total_sec // 60)
                                s = total_sec % 60
                                out.append(f"[{m:02d}:{s:05.2f}] {txt}")
                            elif txt:
                                out.append(txt)

                        full_text = clean_lrc_metadata("\n".join(out))

                        # Валидация тегов внутри текста LRC ([ti:...])
                        ti_m = re.search(r"\[ti:\s*(.+?)\]", full_text, re.I)
                        if ti_m:
                            ti_found = re.sub(r"[^\w]", "", clean_track_title(ti_m.group(1)).lower())
                            if (
                                ti_found != target_norm
                                and (not target_base_norm or ti_found != target_base_norm)
                                and SequenceMatcher(None, target_norm, ti_found).ratio() < 0.7
                            ):
                                log.warning("  ⚠️ lrcmux LRC содержит заголовок [ti:%s] вместо '%s' — отклонено", ti_m.group(1), title)
                                continue

                        if full_text:
                            log.info("  ✓ lrcmux: получено %d строк (%s)", len(lines), "караоке" if has_sync else "plain")
                            return LyricResult(
                                text=full_text,
                                is_synced=has_sync,
                                source="lrcmux",
                                track_title=title,
                                artist=artist,
                            )
                else:
                    log.info("  lrcmux: ответ HTTP %d (текст не найден)", r.status_code)
            except Exception as e:
                log.debug("lrcmux fetch error for '%s — %s': %s", artist, q_title, e)

        log.info("  lrcmux: подходящих текстов не найдено")
        return None

    # ── 4. SyncedLyrics (NetEase, Megalobiz, Musixmatch) ─────────────────────

    def _fetch_syncedlyrics(self, artist: str, title: str) -> LyricResult | None:
        """Ищет синхронизированный караоке-текст через провайдеры syncedlyrics с валидацией названия трека."""
        search_query = f"{artist} {title}".strip()
        if not search_query:
            return None

        log.info("Поиск караоке [SyncedLyrics]: '%s — %s'", artist, title)

        clean_t = clean_track_title(title)
        target_norm = re.sub(r"[^\w]", "", clean_t.lower())
        target_base = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", clean_t).strip()
        target_base_norm = re.sub(r"[^\w]", "", target_base.lower())

        target_base_full = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title).strip()
        queries = [search_query]
        if target_base_full and target_base_full != title:
            base_q = f"{artist} {target_base_full}".strip()
            if base_q not in queries:
                queries.append(base_q)
        if clean_t and clean_t != title:
            cl_q = f"{artist} {clean_t}".strip()
            if cl_q not in queries:
                queries.append(cl_q)

        try:
            import syncedlyrics
            import syncedlyrics.utils
            from syncedlyrics.providers.base import TimeoutSession

            # Увеличиваем таймаут подключения до 5 секунд для надёжной связи с зарубежными базами
            def _custom_timeout_request(session_self, method, url, **kwargs):
                kwargs["timeout"] = (5, 12)
                return super(TimeoutSession, session_self).request(method, url, **kwargs)

            TimeoutSession.request = _custom_timeout_request

            # Перехватываем get_best_match в syncedlyrics, чтобы отсечь треки с другими названиями
            orig_get_best_match = syncedlyrics.utils.get_best_match

            def _strict_get_best_match(results, search_term, compare_key="name", min_score=65):
                valid = []
                for r in results:
                    c_title = ""
                    if isinstance(r, dict):
                        if "track" in r and isinstance(r["track"], dict):
                            c_title = r["track"].get("track_name", "")
                        elif "name" in r:
                            c_title = r.get("name", "")
                        elif "trackName" in r:
                            c_title = r.get("trackName", "")
                        elif "title" in r:
                            c_title = r.get("title", "")
                    elif hasattr(r, "get_text"):
                        c_title = r.get_text()

                    if c_title:
                        c_clean = clean_track_title(c_title).lower()
                        c_norm = re.sub(r"[^\w]", "", c_clean)
                        c_base_norm = re.sub(r"[^\w]", "", re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", c_clean).strip())
                        matched = (
                            c_norm == target_norm
                            or (target_base_norm and c_base_norm == target_base_norm)
                            or (target_norm in c_norm if len(target_norm) >= 4 else False)
                            or (c_norm in target_norm if len(c_norm) >= 4 else False)
                            or SequenceMatcher(None, target_norm, c_norm).ratio() >= 0.75
                            or (
                                target_base_norm
                                and c_base_norm
                                and SequenceMatcher(None, target_base_norm, c_base_norm).ratio() >= 0.75
                            )
                        )
                        if not matched:
                            continue
                    valid.append(r)

                if not valid:
                    return None
                return orig_get_best_match(valid, search_term, compare_key=compare_key, min_score=min_score)

            syncedlyrics.utils.get_best_match = _strict_get_best_match
            try:
                for q in queries:
                    log.info("  Запрос SyncedLyrics: '%s'", q)
                    lrc_text = syncedlyrics.search(q, synced_only=True)
                    if lrc_text and has_timestamps(lrc_text):
                        # Проверка [ti:...] в тексте LRC
                        ti_m = re.search(r"\[ti:\s*(.+?)\]", lrc_text, re.I)
                        if ti_m:
                            ti_found = re.sub(r"[^\w]", "", clean_track_title(ti_m.group(1)).lower())
                            if (
                                ti_found != target_norm
                                and (not target_base_norm or ti_found != target_base_norm)
                                and SequenceMatcher(None, target_norm, ti_found).ratio() < 0.75
                            ):
                                log.warning(
                                    "  ⚠️ SyncedLyrics LRC содержит [ti:%s] вместо '%s' — отклонено",
                                    ti_m.group(1),
                                    title,
                                )
                                continue

                        cleaned_lrc = clean_lrc_metadata(lrc_text)
                        log.info("  ✓ SyncedLyrics: получено караоке (%d строк)", len(cleaned_lrc.splitlines()))
                        return LyricResult(
                            text=cleaned_lrc,
                            is_synced=True,
                            source="syncedlyrics",
                            track_title=title,
                            artist=artist,
                        )
            finally:
                syncedlyrics.utils.get_best_match = orig_get_best_match
        except Exception as e:
            log.debug("syncedlyrics error for '%s — %s': %s", artist, title, e)

        log.info("  SyncedLyrics: караоке не найдено")
        return None

    # ── Каскадный поиск ───────────────────────────────────────────────────────

    def find_lyrics(
        self,
        artist: str,
        title: str,
        album: str = "",
        duration: int = 0,
        preferred_source: str | None = None,
    ) -> LyricResult | None:
        """Выполняет поиск текста по каскаду или по указанному источнику."""
        # Обогащаем список артистов фитами из названия трека, если они там указаны
        title_feats = extract_featured_artists(title)
        if title_feats:
            current_tokens = [a.strip().lower() for a in re.split(r"[,;&]|\s+(?:feat\.?|ft\.?|x|and)\s+", artist, flags=re.I) if a.strip()]
            missing_feats = [f for f in title_feats if f.lower() not in current_tokens]
            if missing_feats:
                artist = f"{artist}, {', '.join(missing_feats)}"
        # 1. Точечный поиск по выбранному сервису
        if preferred_source == "lrclib":
            synced, plain = self._fetch_lrclib(artist, title, album, duration)
            return synced or plain
        if preferred_source == "lrcmux":
            return self._fetch_lrcmux(artist, title, album, duration)
        if preferred_source == "syncedlyrics":
            return self._fetch_syncedlyrics(artist, title)
        if preferred_source == "genius":
            return self._fetch_genius(artist, title)

        # 2. Автоматический каскад:
        # Фаза 1: Синхронизированный текст (LRCLIB)
        lrclib_synced, lrclib_plain = self._fetch_lrclib(artist, title, album, duration)
        if lrclib_synced:
            log.info("  ✓ Найдено караоке [LRCLIB]: %s — %s", artist, title)
            return lrclib_synced

        # Фаза 2: Синхронизированный текст через мультиплексор lrcmux (KuGou, Musixmatch, NetEase, YouTube Music)
        lrcmux_res = self._fetch_lrcmux(artist, title, album, duration)
        if lrcmux_res and lrcmux_res.is_synced:
            log.info("  ✓ Найдено караоке [lrcmux]: %s — %s", artist, title)
            return lrcmux_res

        # Фаза 3: Синхронизированный текст через мульти-провайдер syncedlyrics
        synced_res = self._fetch_syncedlyrics(artist, title)
        if synced_res and synced_res.is_synced:
            log.info("  ✓ Найдено караоке [SyncedLyrics]: %s — %s", artist, title)
            return synced_res

        # Фаза 4: Обычный текст (Plain) — из первого запроса LRCLIB
        if lrclib_plain:
            log.info("  ✓ Найден текст [LRCLIB]: %s — %s", artist, title)
            return lrclib_plain

        # Фаза 5: Обычный текст из lrcmux (если не было синхронизации)
        if lrcmux_res and lrcmux_res.text:
            log.info("  ✓ Найден текст [lrcmux]: %s — %s", artist, title)
            return lrcmux_res

        # Фаза 6: Эталонный plain-текст с Genius
        genius_plain = self._fetch_genius(artist, title)
        if genius_plain:
            log.info("  ✓ Найден текст [Genius]: %s — %s", artist, title)
            return genius_plain

        log.info("  ❌ Текст не найден: %s — %s", artist, title)
        return None

    # ── Обработка файлов ──────────────────────────────────────────────────────

    def process_file(
        self,
        filepath: str | Path,
        overwrite: bool = False,
        smart_upgrade: bool = True,
    ) -> str:
        """Скачивает текст по водопаду и сохраняет .lrc файл рядом с треком.

        Args:
            filepath: путь к FLAC файлу.
            overwrite: если True, выполняет принудительный сетевой поиск и обновление.
            smart_upgrade: если True, защищает существующее караоке (🎤) от даунгрейда до plain (📄).

        Returns:
            Строку-статус для отчёта:
            "Синхронизированный [LRCLIB] 🎤" | "Обычный текст [Genius] 📄" | "Уже есть 📁" | "Не найдено ❌"
        """
        path = Path(filepath)
        if path.suffix.lower() != ".flac":
            return "Не аудиофайл"

        lrc_path = path.with_suffix(".lrc")
        existing_has_sync = False
        if lrc_path.exists():
            try:
                content = lrc_path.read_text(encoding="utf-8")
                existing_has_sync = has_timestamps(content)
            except Exception:
                pass

            # Если перезапись выключена:
            # - Если уже есть караоке -> пропускаем
            # - Если есть обычный текст, но smart_upgrade=False -> пропускаем
            if not overwrite and (existing_has_sync or not smart_upgrade):
                return "Уже есть 📁"

        try:
            audio = FLAC(str(path))
            arts = [a.strip() for a in audio.get("artist", []) if a.strip()]
            title = audio.get("title", [""])[0]
            album = audio.get("album", [""])[0]
            duration = int(audio.info.length) if hasattr(audio.info, "length") else 0

            # Дополнительно извлекаем фиты из названия или имени файла, если их ещё нет в теге artist
            title_feats = extract_featured_artists(title) or extract_featured_artists(path.stem)
            for f_art in title_feats:
                if not any(f_art.lower() == a.lower() for a in arts):
                    arts.append(f_art)

            artist = ", ".join(arts) if arts else audio.get("albumartist", [""])[0]

            if not artist or not title:
                return "Нет тегов ❌"

            res = self.find_lyrics(artist, title, album, duration)
            if res and res.text:
                # Защита от даунгрейда: если уже было караоке, а пришёл обычный текст — сохраняем караоке!
                if existing_has_sync and not res.is_synced:
                    return "Сохранено караоке 🎤 (в сети только обычный)"

                # Если уже был обычный текст и в сети снова только обычный текст (нет караоке)
                # при выключенном overwrite — не перезаписываем файл и не считаем его добавленным
                if lrc_path.exists() and not overwrite and not res.is_synced:
                    return "Уже есть 📁"

                lrc_path.write_text(res.text, encoding="utf-8")
                try:
                    lrc_path.chmod(0o664)
                    os.utime(str(path), None)
                except Exception:
                    pass
                source_labels = {
                    "lrclib": "LRCLIB",
                    "lrcmux": "LRCMUX",
                    "syncedlyrics": "SyncedLyrics",
                    "genius": "Genius",
                    "manual": "Вручную",
                }
                source_label = source_labels.get(res.source, res.source.upper())

                if lrc_path.exists() and res.is_synced and not existing_has_sync:
                    prefix = "Обновлено до караоке"
                elif existing_has_sync:
                    prefix = "Обновлено караоке"
                else:
                    prefix = "Синхронизированный" if res.is_synced else "Обычный текст"

                icon = "🎤" if res.is_synced else "📄"
                return f"{prefix} [{source_label}] {icon}"

            if existing_has_sync:
                return "Сохранено караоке 🎤"
            elif lrc_path.exists():
                return "Уже есть 📁"

            return "Не найдено ❌"
        except Exception as e:
            log.error("Ошибка при обработке текста для %s: %s", path.name, e)
            return f"Ошибка: {e}"

    def save_manual_lyrics(self, lrc_path: Path, text: str) -> LyricResult:
        """Сохраняет вручную введённый текст в файл .lrc."""
        text = text.strip()
        is_sync = has_timestamps(text)
        lrc_path.write_text(text, encoding="utf-8")
        try:
            lrc_path.chmod(0o664)
            flac_path = lrc_path.with_suffix(".flac")
            if flac_path.exists():
                os.utime(str(flac_path), None)
        except Exception:
            pass
        return LyricResult(text=text, is_synced=is_sync, source="manual")


def audit_library_lyrics(library_dir: str | Path) -> dict:
    """Аудит наличия и синхронизации текстов песен по всей музыкальной библиотеке.

    Returns:
        Словарь с ключами:
        - total: общее число FLAC треков
        - synced: список Path треков с синхронизированным .lrc
        - plain: список Path треков с обычным текстом .lrc (plain)
        - missing: список Path треков без .lrc
        - albums_plain: словарь {album_dir_name: [Path, ...]} для альбомов с обычным текстом
    """
    root = Path(library_dir)
    if not root.exists():
        return {"total": 0, "synced": [], "plain": [], "missing": [], "albums_plain": {}}

    flac_files = sorted(root.rglob("*.flac"))
    synced: list[Path] = []
    plain: list[Path] = []
    missing: list[Path] = []
    albums_plain: dict[str, list[Path]] = {}

    for flac in flac_files:
        lrc = flac.with_suffix(".lrc")
        if not lrc.exists():
            missing.append(flac)
            continue

        try:
            content = lrc.read_text(encoding="utf-8", errors="ignore")
            if has_timestamps(content):
                synced.append(flac)
            elif content.strip():
                plain.append(flac)
                alb_name = flac.parent.name
                albums_plain.setdefault(alb_name, []).append(flac)
            else:
                missing.append(flac)
        except Exception:
            missing.append(flac)

    return {
        "total": len(flac_files),
        "synced": synced,
        "plain": plain,
        "missing": missing,
        "albums_plain": albums_plain,
    }
