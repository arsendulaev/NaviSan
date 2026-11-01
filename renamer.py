"""Управление именами файлов и папок релиза (ADR-0007).

Приводит структуру файлов к единообразному формату Navidrome:
  - Имена треков: "{track_number}. {safe_title}.flac" (с дополнением нулями).
  - Имя папки: "{Artist} - {Album}".
  - Защита от недопустимых символов файловой системы (/:*?"<>|).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from collections import Counter
from pathlib import Path

from mutagen.flac import FLAC

log = logging.getLogger(__name__)


class FileSystemManager:
    """Нормализация имён треков и папок альбомов на файловой системе."""

    INVALID_CHARS = re.compile(r'[\\/*?:"<>|]')

    def clean_name(self, name: str) -> str:
        """Удаляет запрещённые символы файловой системы и лишние пробелы."""
        return self.INVALID_CHARS.sub("", name).strip()

    def rename_file(self, filepath: str | Path) -> str:
        """Переименовывает трек в формат '01. Title.flac'.

        Args:
            filepath: Текущий путь к FLAC-файлу.

        Returns:
            Новый (или неизменённый) строковый путь к файлу.
        """
        path = Path(filepath)
        if not path.exists() or path.suffix.lower() != ".flac":
            return str(filepath)

        try:
            audio = FLAC(str(path))
        except Exception as e:
            log.warning("Не удалось прочитать теги для переименования %s: %e", path.name, e)
            return str(filepath)

        track_raw = audio.get("tracknumber", [""])[0].split("/")[0].strip()
        track_num = track_raw.zfill(2) if track_raw.isdigit() else ""

        title = audio.get("title", ["Unknown_Title"])[0]
        safe_title = self.clean_name(title)

        new_filename = f"{track_num}. {safe_title}.flac" if track_num else f"{safe_title}.flac"
        new_path = path.parent / new_filename

        if path != new_path:
            path.rename(new_path)
            # Если рядом был .lrc файл со старым именем, переименовываем его тоже
            old_lrc = path.with_suffix(".lrc")
            new_lrc = new_path.with_suffix(".lrc")
            if old_lrc.exists():
                try:
                    old_lrc.rename(new_lrc)
                except Exception as e:
                    log.warning("Не удалось переименовать .lrc: %s", e)
            log.debug("Файл переименован: %s -> %s", path.name, new_filename)
            return str(new_path)

        return str(filepath)

    def rename_album_folder(
        self, folder_path: str | Path, force_common_artists: bool = False
    ) -> str:
        """Анализирует все FLAC-треки в папке и переименовывает её в 'Artist - Album'.

        Args:
            folder_path: Путь к папке альбома.
            force_common_artists: Если True, игнорирует тег риппера и берёт артистов,
                присутствующих во всех треках альбома, сортируя их по алфавиту.

        Returns:
            Новый (или неизменённый) строковый путь к папке.
        """
        folder = Path(folder_path)
        flac_files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() == ".flac"]
        if not flac_files:
            return str(folder_path)

        album_artists_list: list[tuple[str, ...]] = []
        track_artists_sets: list[set[str]] = []
        album_names: list[str] = []

        # 1. Сбор метаданных по всем трекам альбома
        for f in flac_files:
            try:
                audio = FLAC(str(f))
            except Exception:
                continue

            aa = audio.get("albumartist", [])
            if aa:
                album_artists_list.append(tuple(aa))

            a = audio.get("artist", [])
            if a:
                track_artists_sets.append(set(a))

            al = audio.get("album", audio.get("title", ["Unknown_Release"]))
            album_names.append(al[0])

        # 2. Определение канонического названия альбома
        best_album = Counter(album_names).most_common(1)[0][0] if album_names else "Unknown_Release"

        # 3. Определение артиста(ов) релиза
        if force_common_artists and track_artists_sets:
            common = set.intersection(*track_artists_sets)
            if common:
                final_artists = sorted(common, key=lambda s: s.casefold())
            elif album_artists_list:
                best_aa = Counter(album_artists_list).most_common(1)[0][0]
                final_artists = sorted(best_aa, key=lambda s: s.casefold())
            else:
                final_artists = ["Various Artists"]
        elif album_artists_list:
            best_aa = Counter(album_artists_list).most_common(1)[0][0]
            final_artists = list(best_aa)
        elif track_artists_sets:
            common = set.intersection(*track_artists_sets)
            if common:
                try:
                    first_track_artists = FLAC(str(flac_files[0])).get("artist", [])
                    final_artists = [art for art in first_track_artists if art in common]
                except Exception:
                    final_artists = list(common)
            else:
                final_artists = ["Various Artists"]
        else:
            final_artists = ["Unknown Artist"]

        # 4. Формирование нового имени папки
        artists_str = ", ".join(final_artists)
        new_dirname = self.clean_name(f"{artists_str} - {best_album}")
        new_folder = folder.parent / new_dirname

        # 5. Проверка необходимости переименования
        if folder == new_folder:
            return str(folder_path)

        # Защита от дурака при изменении только регистра
        if folder.name.lower() == new_folder.name.lower():
            temp_path = folder.parent / f"{folder.name}_temp_rename"
            folder.rename(temp_path)
            temp_path.rename(new_folder)
            log.info("Папка переименована (регистр): %s", new_dirname)
            return str(new_folder)

        # Если целевая папка уже существует — объединяем с ней содержимое
        if new_folder.exists():
            log.info("Папка с именем '%s' уже существует рядом. Объединяю содержимое.", new_dirname)
            self.merge_folders(folder, new_folder)
            return str(new_folder)

        folder.rename(new_folder)
        log.info("Папка переименована: %s", new_dirname)
        return str(new_folder)

    def merge_folders(
        self,
        src_folder: str | Path,
        dst_folder: str | Path,
        has_chosen_cover: bool = False,
    ) -> dict:
        """Объединяет файлы из src_folder в dst_folder без создания дубликатов (2).

        - Переносит новые треки и метаданные.
        - Заменяет существующие FLAC обновлёнными версиями.
        - Не даунгрейдит синхронизированные тексты (.lrc).
        - Вшивает обложку альбома в добавленные треки (если у них нет обложки).
        - Пересчитывает ReplayGain для объединённого релиза.
        - Удаляет исходную папку src_folder.

        Returns:
            Словарь со статистикой слияния:
            {"added_tracks": [...], "updated_tracks": [...], "total_flacs": int, "rg_ok": bool, "rg_msg": str}
        """
        src = Path(src_folder)
        dst = Path(dst_folder)
        dst.mkdir(parents=True, exist_ok=True)

        added_tracks: list[Path] = []
        updated_tracks: list[Path] = []

        # 1. Рекурсивный перенос файлов
        for root, dirs, files in os.walk(src):
            rel_dir = Path(root).relative_to(src)
            target_dir = dst / rel_dir
            target_dir.mkdir(parents=True, exist_ok=True)

            for fname in files:
                src_file = Path(root) / fname
                dst_file = target_dir / fname
                ext = src_file.suffix.lower()

                if ext == ".flac":
                    if dst_file.exists():
                        try:
                            dst_file.unlink(missing_ok=True)
                        except Exception as e:
                            log.warning("Не удалось удалить существующий файл %s: %s", dst_file.name, e)
                        shutil.move(str(src_file), str(dst_file))
                        updated_tracks.append(dst_file)
                    else:
                        shutil.move(str(src_file), str(dst_file))
                        added_tracks.append(dst_file)

                elif ext == ".lrc":
                    if dst_file.exists():
                        try:
                            dst_txt = dst_file.read_text(encoding="utf-8", errors="replace")
                            src_txt = src_file.read_text(encoding="utf-8", errors="replace")
                            # Не даунгрейдим синхронизированный текст в обычный
                            dst_has_ts = bool(re.search(r"\[\d{2}:\d{2}", dst_txt))
                            src_has_ts = bool(re.search(r"\[\d{2}:\d{2}", src_txt))
                            if dst_has_ts and not src_has_ts:
                                src_file.unlink(missing_ok=True)
                            else:
                                dst_file.unlink(missing_ok=True)
                                shutil.move(str(src_file), str(dst_file))
                        except Exception:
                            dst_file.unlink(missing_ok=True)
                            shutil.move(str(src_file), str(dst_file))
                    else:
                        shutil.move(str(src_file), str(dst_file))

                elif fname.lower() in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg"):
                    if dst_file.exists():
                        if has_chosen_cover:
                            dst_file.unlink(missing_ok=True)
                            shutil.move(str(src_file), str(dst_file))
                        else:
                            src_file.unlink(missing_ok=True)
                    else:
                        shutil.move(str(src_file), str(dst_file))

                else:
                    if not dst_file.exists():
                        shutil.move(str(src_file), str(dst_file))
                    else:
                        src_file.unlink(missing_ok=True)

        # 2. Удаляем исходную папку
        shutil.rmtree(src, ignore_errors=True)

        # 3. Синхронизируем обложку альбома в добавленные треки
        dst_cover = None
        for cname in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg"):
            cp = dst / cname
            if cp.exists() and cp.stat().st_size > 0:
                dst_cover = cp
                break

        if dst_cover:
            try:
                from surgeon import FLACSurgeon
                surgeon = FLACSurgeon()
                target_list = list(dst.rglob("*.flac")) if has_chosen_cover else added_tracks
                for tr in target_list:
                    if tr.exists():
                        try:
                            audio = FLAC(str(tr))
                            if not audio.pictures or has_chosen_cover:
                                surgeon.heal_file(str(tr), force_update=has_chosen_cover)
                        except Exception as e:
                            log.debug("Не удалось синхронизировать обложку в %s: %s", tr.name, e)
            except Exception as e:
                log.warning("Ошибка при вшивании обложки после слияния: %s", e)

        # 4. Пересчёт / удаление ReplayGain для объединённого релиза
        rg_ok = False
        rg_msg = ""
        try:
            from settings import get_replaygain_mode
            rg_mode = get_replaygain_mode()
            if rg_mode == "strip":
                from replaygain import remove_replaygain
                remove_replaygain(dst)
            elif rg_mode == "write":
                from replaygain import apply_replaygain
                rg_ok, rg_msg = apply_replaygain(dst)
        except Exception as e:
            log.warning("Ошибка обработки ReplayGain после слияния: %s", e)

        total_flacs = len(list(dst.rglob("*.flac")))

        return {
            "dest": dst,
            "added_tracks": [t.name for t in added_tracks],
            "updated_tracks": [t.name for t in updated_tracks],
            "total_flacs": total_flacs,
            "rg_ok": rg_ok,
            "rg_msg": rg_msg,
        }
