"""Вшивание обложек, очистка тегов и синхронизация метаданных FLAC (ADR-0006).

Выполняет хирургическую обработку аудиофайлов:
  - Whitelist фильтрация разрешённых тегов и удаление мусорных полей.
  - Извлечение фитов (feat./ft.) из названия трека и добавление их в тег ARTIST.
  - Санитизация имён артистов через TagSanitizer.
  - Вшивание cover.jpg в FLAC-контейнер.
  - Синхронизация полей DATE и YEAR.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from mutagen.flac import FLAC, Picture

from sanitizer import TagSanitizer

log = logging.getLogger(__name__)


def clean_date_str(s: str) -> str:
    """Очищает и нормализует строку даты к формату YYYY-MM-DD или YYYY."""
    if not s:
        return ""
    # Отрезаем временную метку (T00:00:00Z или пробел)
    s = s.split("T")[0].split(" ")[0].strip()
    s = s.replace("/", "-")
    if re.match(r"^[0\-\./]+$", s):
        return ""
    m_eu = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", s)
    if m_eu:
        s = f"{m_eu.group(3)}-{m_eu.group(2)}-{m_eu.group(1)}"
    m_eu_dash = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", s)
    if m_eu_dash:
        s = f"{m_eu_dash.group(3)}-{m_eu_dash.group(2)}-{m_eu_dash.group(1)}"
    m_dot = re.match(r"^(\d{4})\.(\d{2})\.(\d{2})$", s)
    if m_dot:
        s = f"{m_dot.group(1)}-{m_dot.group(2)}-{m_dot.group(3)}"
    s = re.sub(r"[-./]00[-./]00$", "", s)
    s = re.sub(r"[-./]00$", "", s)
    return s


def extract_year_and_date(val: str | None) -> tuple[str, str]:
    """Извлекает 4-значный год (для DATE / Mp3tag Year) и полную дату (для ORIGINALDATE).

    Returns:
        (year_4, full_date)
    """
    clean_d = clean_date_str(val)
    if not clean_d:
        return "", ""
    m_year = re.search(r"\b(19\d{2}|20\d{2})\b", clean_d)
    if m_year:
        year_4 = m_year.group(1)
    elif len(clean_d) >= 4 and clean_d[:4].isdigit():
        year_4 = clean_d[:4]
    else:
        year_4 = clean_d
    return year_4, clean_d


def parse_genres(raw_genres: str | list[str] | None) -> list[str]:
    """Разбивает строки жанров (Hip-Hop/Rap, Pop\\Rock, Dance, House) на список уникальных жанров."""
    if not raw_genres:
        return []
    if isinstance(raw_genres, str):
        raw_list = [raw_genres]
    else:
        raw_list = list(raw_genres)

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_list:
        parts = re.split(r"[/;]|\\+|\s*,\s*", str(item))
        for part in parts:
            clean = part.strip()
            if not clean:
                continue
            if clean.lower() == "hip hop":
                clean = "Hip-Hop"
            if clean.lower() not in seen:
                seen.add(clean.lower())
                result.append(clean)
    return result


class FLACSurgeon:
    """Очистка метаданных и вшивание обложек в FLAC-файлы."""

    # Tag Whitelist по ADR-0006
    WHITELIST: frozenset[str] = frozenset({
        "title", "artist", "albumartist", "album",
        "date", "originaldate", "tracknumber", "tracktotal",
        "discnumber", "disctotal", "genre", "label",
        "itunesadvisory",
        "replaygain_track_gain", "replaygain_track_peak",
        "replaygain_album_gain", "replaygain_album_peak",
        "replaygain_reference_loudness",
    })

    REPLAYGAIN_TAGS: frozenset[str] = frozenset({
        "replaygain_track_gain", "replaygain_track_peak",
        "replaygain_album_gain", "replaygain_album_peak",
        "replaygain_reference_loudness",
    })

    def __init__(
        self,
        sanitizer: TagSanitizer | None = None,
        strip_replaygain: bool | None = None,
    ) -> None:
        self.sanitizer = sanitizer if sanitizer is not None else TagSanitizer()
        if strip_replaygain is None:
            try:
                from settings import get_replaygain_mode
                self.strip_replaygain = (get_replaygain_mode() == "strip")
            except Exception:
                self.strip_replaygain = False
        else:
            self.strip_replaygain = strip_replaygain

    def _embed_cover_from_file(
        self,
        audio: FLAC,
        folder_path: str | Path,
        force_update: bool = False,
    ) -> None:
        """Вшивает обложку из локального cover.jpg, если он существует (ADR-0012)."""
        if force_update:
            audio.clear_pictures()

        if audio.pictures:
            return

        cover_path = Path(folder_path) / "cover.jpg"
        if not cover_path.exists():
            return

        try:
            img_data = cover_path.read_bytes()
            if not img_data:
                return

            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Front Cover"
            pic.data = img_data
            audio.add_picture(pic)
            log.debug("Обложка вшита из %s", cover_path.name)
        except OSError as e:
            log.warning("Не удалось прочитать обложку %s: %s", cover_path, e)

    def heal_file(self, filepath: str | Path, force_update: bool = False) -> None:
        """Главный конвейер очистки метаданных FLAC-файла.

        Args:
            filepath: Путь к обрабатываемому .flac файлу.
            force_update: Перезаписывать ли обложку, даже если она уже вшита.
        """
        path = Path(filepath)
        if path.suffix.lower() != ".flac":
            return

        try:
            audio = FLAC(str(path))
        except Exception as e:
            log.error("Файл повреждён или не читается: %s (%s)", path.name, e)
            return

        # 0. Нормализация тегов лейбла (ORGANIZATION / PUBLISHER -> LABEL)
        if "label" not in audio:
            for alt in ("organization", "publisher", "recordlabel", "record_label"):
                if alt in audio:
                    audio["label"] = audio[alt]
                    break

        # Нормализация тегов Explicit (EXPLICIT / ADVISORY -> ITUNESADVISORY)
        if "itunesadvisory" not in audio:
            for alt in ("explicit", "explicitlyrics", "explicit_lyrics", "advisory", "rating_advisory"):
                if alt in audio:
                    val = str(audio[alt][0]).strip().lower()
                    if val in ("1", "true", "yes", "explicit", "e"):
                        audio["itunesadvisory"] = ["1"]
                    elif val in ("2", "clean", "c"):
                        audio["itunesadvisory"] = ["2"]
                    elif val in ("0", "false", "no", "none"):
                        audio["itunesadvisory"] = ["0"]
                    break
        elif "itunesadvisory" in audio:
            val = str(audio["itunesadvisory"][0]).strip().lower()
            if val in ("1", "true", "yes", "explicit", "e"):
                audio["itunesadvisory"] = ["1"]
            elif val in ("2", "clean", "c"):
                audio["itunesadvisory"] = ["2"]
            elif val in ("0", "false", "no", "none"):
                audio["itunesadvisory"] = ["0"]

        # 0. Миграция YEAR в ORIGINALDATE / DATE перед whitelist-фильтрацией
        raw_year_pre = audio.get("year", [""])[0].strip()
        raw_date_pre = audio.get("date", [""])[0].strip()
        raw_orig_pre = audio.get("originaldate", [""])[0].strip()
        if raw_year_pre:
            if not raw_orig_pre:
                audio["originaldate"] = [raw_year_pre]
            if not raw_date_pre:
                audio["date"] = [raw_year_pre]

        # Миграция устаревших алиасов (totaltracks -> tracktotal, totaldiscs -> disctotal)
        if "totaltracks" in audio and "tracktotal" not in audio:
            audio["tracktotal"] = audio["totaltracks"]
        if "totaldiscs" in audio and "disctotal" not in audio:
            audio["disctotal"] = audio["totaldiscs"]
        if "track" in audio and "tracknumber" not in audio:
            audio["tracknumber"] = audio["track"]
        if "disc" in audio and "discnumber" not in audio:
            audio["discnumber"] = audio["disc"]

        # Нормализация дробей вида <номер>/<всего> в отдельные теги (стандарт Vorbis Comment)
        raw_tn = str(audio.get("tracknumber", [""])[0]).strip()
        if "/" in raw_tn:
            tn_num, _, tn_tot = raw_tn.partition("/")
            tn_num = tn_num.strip()
            tn_tot = tn_tot.strip()
            if tn_num:
                audio["tracknumber"] = [str(int(tn_num))] if tn_num.isdigit() else [tn_num]
            if tn_tot and tn_tot.isdigit() and int(tn_tot) > 0 and not audio.get("tracktotal"):
                audio["tracktotal"] = [str(int(tn_tot))]
        elif raw_tn and raw_tn.isdigit():
            audio["tracknumber"] = [str(int(raw_tn))]

        raw_tt = str(audio.get("tracktotal", [""])[0]).strip()
        if "/" in raw_tt:
            raw_tt = raw_tt.split("/")[-1].strip()
        if raw_tt and raw_tt.isdigit() and int(raw_tt) > 0:
            audio["tracktotal"] = [str(int(raw_tt))]

        raw_dn = str(audio.get("discnumber", [""])[0]).strip()
        if "/" in raw_dn:
            dn_num, _, dn_tot = raw_dn.partition("/")
            dn_num = dn_num.strip()
            dn_tot = dn_tot.strip()
            if dn_num:
                audio["discnumber"] = [str(int(dn_num))] if dn_num.isdigit() else [dn_num]
            if dn_tot and dn_tot.isdigit() and int(dn_tot) > 0 and not audio.get("disctotal"):
                audio["disctotal"] = [str(int(dn_tot))]
        elif raw_dn and raw_dn.isdigit():
            audio["discnumber"] = [str(int(raw_dn))]

        raw_dt = str(audio.get("disctotal", [""])[0]).strip()
        if "/" in raw_dt:
            raw_dt = raw_dt.split("/")[-1].strip()
        if raw_dt and raw_dt.isdigit() and int(raw_dt) > 0:
            audio["disctotal"] = [str(int(raw_dt))]

        # Fallback: номер трека из имени файла (если отсутствует в тегах)
        if not audio.get("tracknumber") or audio.get("tracknumber") in (["0"], ["00"]):
            m_fn = re.match(r"^(\d{1,3})[\s\.\-_]", path.name)
            if m_fn:
                audio["tracknumber"] = [str(int(m_fn.group(1)))]

        # Гарантируем корректный номер диска
        if not audio.get("discnumber") or audio.get("discnumber") in (["0"], ["00"]):
            audio["discnumber"] = ["1"]
        if not audio.get("disctotal") or audio.get("disctotal") in (["0"], ["00"]):
            audio["disctotal"] = ["1"]

        # 1. Фильтрация по whitelist тегов и очистка мусорных значений
        allowed_tags = (
            self.WHITELIST - self.REPLAYGAIN_TAGS
            if self.strip_replaygain
            else self.WHITELIST
        )
        saved_tags: dict[str, list[str]] = {}
        for key in list(audio.keys()):
            lower_key = key.lower()
            if lower_key in allowed_tags:
                cleaned_vals: list[str] = []
                for v in audio[key]:
                    v_str = str(v).strip()
                    if not v_str:
                        continue
                    if lower_key in ("year", "date", "originaldate") and (v_str in ("0", "0000", "0000-00-00", "0-0-0", "00-00-0000") or re.match(r"^[0\-\./]+$", v_str)):
                        continue
                    if lower_key in ("tracktotal", "disctotal") and v_str == "0":
                        continue
                    if lower_key == "discnumber" and v_str == "0":
                        v_str = "1"
                    if lower_key == "label" and v_str.lower() in ("[no label]", "no label"):
                        continue
                    cleaned_vals.append(v_str)

                if cleaned_vals:
                    saved_tags[lower_key] = cleaned_vals

            del audio[key]

        # 2. Восстановление очищенных тегов
        for key, val in saved_tags.items():
            audio[key] = val

        # 3. Извлечение фитов из названия и санитизация артистов
        if "title" not in audio or not audio["title"] or not audio["title"][0].strip():
            raw_title = re.sub(r"^\d{1,3}[\s\.\-_]+", "", path.stem).strip()
            if raw_title:
                audio["title"] = [raw_title]

        extracted_feats = ""
        if "title" in audio:
            title_str = audio["title"][0]

            feat_pattern = re.compile(
                r"(?i)(?:[\[\(](?:feat\.?|ft\.?)\s+([^\]\)]+)[\]\)]|\s+(?:feat\.?|ft\.?)\s+(.+)$)"
            )
            feat_match = feat_pattern.search(title_str)
            if feat_match:
                extracted_feats = feat_match.group(1) or feat_match.group(2)
                title_str = feat_pattern.sub("", title_str)

            prod_pattern = re.compile(r"(?i)[\[\(]prod\..*?[\]\)]")
            title_str = prod_pattern.sub("", title_str)
            audio["title"] = [re.sub(r"\s+", " ", title_str).strip()]

        if "artist" in audio:
            raw = " & ".join(audio["artist"])
            if extracted_feats:
                raw += f" & {extracted_feats}"
            audio["artist"] = self.sanitizer.process_artists(raw)

        if "albumartist" in audio:
            raw = " & ".join(audio["albumartist"])
            audio["albumartist"] = self.sanitizer.process_artists(raw)

        # 3.5. Разбивка мульти-жанров (Hip-Hop/Rap -> ['Hip-Hop', 'Rap'])
        if "genre" in audio:
            parsed_genres = parse_genres(audio["genre"])
            if parsed_genres:
                audio["genre"] = parsed_genres
            else:
                del audio["genre"]

        # 4. Вшивание обложки из cover.jpg
        self._embed_cover_from_file(audio, path.parent, force_update)

        # 5. Нормализация DATE (строго 4 цифры года YYYY) и ORIGINALDATE (полная дата YYYY-MM-DD)
        raw_date = audio.get("date", [""])[0].strip()
        raw_year = audio.get("year", [""])[0].strip()
        raw_orig = audio.get("originaldate", [""])[0].strip()

        clean_date = clean_date_str(raw_date)
        clean_year = clean_date_str(raw_year)
        clean_orig = clean_date_str(raw_orig)

        # Выбираем наиболее полную дату (YYYY-MM-DD имеет приоритет над YYYY)
        best_full_date = ""
        for cand in (clean_orig, clean_date, clean_year):
            if len(cand) > len(best_full_date):
                best_full_date = cand
        if not best_full_date:
            best_full_date = clean_date or clean_orig or clean_year

        if best_full_date:
            year_4, full_d = extract_year_and_date(best_full_date)
            audio["date"] = [year_4]
            audio["originaldate"] = [full_d]
        else:
            if "date" in audio:
                del audio["date"]
            if "originaldate" in audio:
                del audio["originaldate"]

        # Тег YEAR исключён: в плеерах (Mp3tag) колонка "Год" читает Vorbis Comment DATE,
        # а точная дата хранится в ORIGINALDATE
        if "year" in audio:
            del audio["year"]

        # 6. Сохранение изменений
        audio.save()
        log.debug("Файл очищен: %s", path.name)
