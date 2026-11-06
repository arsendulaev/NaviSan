"""Нормализация имён артистов через базу Navidrome.

Разбивает составные строки исполнителей (feat, ft, x, and, запятые, слеши), сверяет их с существующей
базой данных Navidrome (navidrome.db) для сохранения единообразия регистра и предотвращения
дублирования карточек артистов.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

log = logging.getLogger(__name__)


VARIOUS_ARTISTS_NAMES: frozenset[str] = frozenset({
    "various artists",
    "various",
    "va",
    "v/a",
    "v.a.",
    "soundtrack",
    "ost",
})

# Известные группы/исполнители с запятыми, амперсандами и спецсимволами в официальном названии,
# которые нельзя разбивать на отдельных участников
KNOWN_COMPOUND_ARTISTS: frozenset[str] = frozenset({
    "tyler, the creator",
    "earth, wind & fire",
    "crosby, stills, nash & young",
    "emerson, lake & palmer",
    "blood, sweat & tears",
    "simon & garfunkel",
    "hall & oates",
    "kool & the gang",
    "kc & the sunshine band",
    "above & beyond",
    "florence + the machine",
    "young p&h",
    "bell biv devoe",
    "tony! toni! toné!",
    "ac/dc",
})


class TagSanitizer:
    """Санитизация и приведение имён артистов к единому стандарту."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        cache_path: str | Path | None = None,
    ) -> None:
        self.db_path = self._resolve_db_path(db_path)
        # Паттерн для сплита артистов:
        # 1. feat / ft / x / and / и / & (с пробелами вокруг)
        # 2. запятые и точки с запятой
        # 3. двойной или одиночный обратный слеш (\\ или \)
        # 4. прямой слеш с пробелами вокруг ( / ) — чтобы не ломать AC/DC
        self.split_pattern = re.compile(
            r"(?i)\s+(?:feat\.?|ft\.?|x|and|и|&)\s+|\s*[,;]\s*|\s*\\\\\s*|\s*\\\s*|\s+/\s+"
        )
        self._verify_db_connection()

    def _resolve_db_path(self, user_db_path: str | Path | None) -> str:
        """Определяет актуальный путь к базе Navidrome с автопоиском."""
        candidates: list[str] = []

        if user_db_path:
            candidates.append(str(user_db_path))

        env_val = os.environ.get("NAVISAN_DB")
        if env_val:
            candidates.append(env_val)

        # Стандартные пути (внутри Docker, в текущей папке, рядом с кодом, локальный dev)
        candidates.extend([
            "/data/navidrome.db",
            "navidrome.db",
            str(Path(__file__).parent / "navidrome.db"),
            "/home/arsen/Downloads/navidrome.db",
        ])

        for cand in candidates:
            if cand and os.path.exists(cand):
                return cand

        # Если файл нигде не найден, возвращаем первый предложенный или /data/navidrome.db
        return candidates[0] if candidates else "/data/navidrome.db"

    def _connect(self) -> sqlite3.Connection:
        """Создаёт безопасное read-only подключение к базе Navidrome.

        База Navidrome работает в режиме WAL (Write-Ahead Logging).
        При монтировании одиночного файла базы в контейнер с флагом :ro
        стандартный sqlite3.connect пытается открыть файл на запись для
        создания -shm/-wal файлов и падает с ошибкой:
        'attempt to write a readonly database'.
        Параметр mode=ro&immutable=1 сообщает SQLite, что файл read-only,
        и отключает попытки создания вспомогательных файлов блокировок.
        """
        abs_path = os.path.abspath(self.db_path)
        try:
            return sqlite3.connect(f"file:{abs_path}?mode=ro&immutable=1", uri=True)
        except Exception:
            return sqlite3.connect(self.db_path)

    def _verify_db_connection(self) -> None:
        """Проверяет доступность базы и выводит информативное сообщение в лог."""
        if not os.path.exists(self.db_path):
            log.warning(
                "TagSanitizer: база Navidrome не найдена (%s). "
                "Сверка имён с базой отключена! Проверьте volume и NAVISAN_DB в .env.",
                self.db_path,
            )
            return

        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT count(*) FROM artist")
                count = cursor.fetchone()[0]
                log.info("TagSanitizer: подключена база Navidrome %s (артистов в базе: %d)", self.db_path, count)
        except Exception as e:
            log.warning("TagSanitizer: ошибка чтения базы Navidrome (%s): %s", self.db_path, e)

    def _get_artist_from_db(self, artist_name: str) -> str | None:
        """Находит каноническое написание артиста в Navidrome.

        Сначала проверяет фактическое большинство в media_file (консенсус тегов),
        затем таблицу artist. Игнорирует устаревший camelCase/нижний регистр,
        если в файлах есть общепринятое написание.
        """
        if not os.path.exists(self.db_path):
            return None

        try:
            with self._connect() as conn:
                cursor = conn.cursor()

                # 1. Поиск наиболее частого написания в media_file среди треков этого артиста
                cursor.execute(
                    """
                    SELECT mf.artist 
                    FROM media_file mf 
                    JOIN media_file_artists mfa ON mfa.media_file_id = mf.id
                    JOIN artist a ON a.id = mfa.artist_id
                    WHERE a.name COLLATE NOCASE = ?
                    """,
                    (artist_name,),
                )
                rows = [r[0] for r in cursor.fetchall() if r[0]]
                tokens: list[str] = []
                for r in rows:
                    for part in r.split(" • "):
                        clean_part = part.strip()
                        if clean_part.lower() == artist_name.lower():
                            tokens.append(clean_part)

                if tokens:
                    # Исключаем написания в нижнем регистре, если есть варианты с заглавной буквы
                    non_lower = [t for t in tokens if not t.islower()]
                    if non_lower:
                        return Counter(non_lower).most_common(1)[0][0]
                    return Counter(tokens).most_common(1)[0][0]

                # 2. Резервный поиск по таблице artist
                cursor.execute(
                    "SELECT name FROM artist WHERE name COLLATE NOCASE = ?",
                    (artist_name,),
                )
                result = cursor.fetchone()
                if result:
                    db_name = str(result[0]).strip()
                    # Не навязываем нижний регистр из artist.name
                    if not db_name.islower():
                        return db_name

        except sqlite3.Error as e:
            log.debug("Ошибка чтения базы Navidrome: %s", e)

        return None

    def _get_compound_artists_from_db(self) -> set[str]:
        """Возвращает имена защищённых групп.

        Берёт встроенный список и дополняет группами с '&' из navidrome.db.
        Исключает ошибочно склеенные через запятую строки, чтобы не блокировать их сплит.
        """
        res = set(KNOWN_COMPOUND_ARTISTS)
        if not os.path.exists(self.db_path):
            return res

        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM artist WHERE (name LIKE '%&%' OR name LIKE '% and %') AND name NOT LIKE '%,%'"
                )
                for r in cursor.fetchall():
                    val = str(r[0]).strip()
                    if val and len(val) < 80:
                        res.add(val.lower())
        except sqlite3.Error as e:
            log.debug("Ошибка чтения составных артистов из Navidrome: %s", e)
        return res

    def process_artists(self, raw_artist_string: str) -> list[str]:
        """Разбивает строку артистов и приводит каждое имя к каноническому виду."""
        raw_str = (raw_artist_string or "").strip()
        if not raw_str:
            return []

        raw_lower = raw_str.lower()
        if raw_lower in VARIOUS_ARTISTS_NAMES:
            return ["Various Artists"]

        # Формируем список защищённых составных артистов
        protected = self._get_compound_artists_from_db()

        # Если вся строка целиком — это защищённая составная группа (например, "Tyler, The Creator")
        if raw_lower in protected:
            db_match = self._get_artist_from_db(raw_str)
            return [db_match or raw_str]

        # 1. Быстрая проверка: полное совпадение с базой Navidrome, ТОЛЬКО если в строке
        # нет стандартных разделителей нескольких исполнителей (запятые, точки с запятой, слеши)
        if not any(sep in raw_str for sep in (",", ";", "\\", "/")):
            db_match = self._get_artist_from_db(raw_str)
            if db_match and not db_match.islower():
                return [db_match]

        # 2. Экранирование защищённых групп токенами перед сплитом
        sorted_prot = sorted(protected, key=len, reverse=True)
        tokens: dict[str, str] = {}
        masked = raw_str

        for p in sorted_prot:
            pattern = re.compile(r"(?<!\w)" + re.escape(p) + r"(?!\w)", re.IGNORECASE)
            def repl(m: re.Match) -> str:
                tok = f"\x00PROT_{len(tokens)}\x00"
                tokens[tok] = m.group(0)
                return tok
            masked = pattern.sub(repl, masked)

        # 3. Разделение по разделителям
        split_artists = self.split_pattern.split(masked)
        clean_artists: list[str] = []

        for artist in split_artists:
            artist = artist.strip()
            if not artist:
                continue

            # Восстанавливаем защищённые токены
            for tok, orig in tokens.items():
                if tok in artist:
                    artist = artist.replace(tok, orig)

            artist_lower = artist.lower()
            if artist_lower in VARIOUS_ARTISTS_NAMES:
                clean_artists.append("Various Artists")
                continue

            # Сверка с базой Navidrome (консенсус медиатеки)
            db_artist = self._get_artist_from_db(artist)
            if db_artist:
                final_artist = db_artist
            else:
                # Если артиста нет в базе и он передан полностью в нижнем регистре,
                # приводим первую букву каждого слова к заглавной (Title Case)
                if artist.islower() and len(artist) > 2:
                    final_artist = artist.title()
                else:
                    final_artist = artist

            clean_artists.append(final_artist)

        return list(dict.fromkeys(clean_artists))

