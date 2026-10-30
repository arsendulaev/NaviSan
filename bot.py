"""Telegram-бот NaviSan (ADR-0013).

Единственный интерфейс к ядру NaviSan. Следит за папкой Inbox (watcher),
запускает pipeline обработки через Staging и ведёт диалог с владельцем:

  Inbox Watcher обнаруживает новый релиз
      ↓
  Бот ищет обложки → присылает нативный Telegram Slideshow (свайп влево/вправо)
      ↓
  Владелец свайпает и нажимает кнопку с номером обложки
      ↓
  Бот удаляет слайдшоу и присылает 1 статусное сообщение,
  которое превращается в финальный отчёт без спама в чат.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from enum import Enum, auto
from pathlib import Path
from dataclasses import dataclass, field, replace

import requests
from dotenv import load_dotenv
from mutagen.flac import FLAC
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest, NetworkError, TimedOut, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from covers import CoverCandidate, search_covers
from lyricist import LyricManager, has_timestamps, preview_lyrics, audit_library_lyrics
from pipeline import (
    album_info,
    apply_cover,
    apply_pipeline,
    cleanup_inbox,
    copy_to_staging,
    count_flacs,
    find_existing_library_album,
    move_to_library,
    update_album_cover,
    get_album_artists_info,
    get_album_artist_suggestions,
    add_track_artist,
    remove_track_artist,
    update_album_artist,
    replace_all_tracks_artist,
    set_track_artists,
    upgrade_library,
    enrich_single_track_release,
)
from watcher import watch_inbox

load_dotenv()

# ── Конфигурация ─────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID: int = int(os.environ["NAVISAN_OWNER_ID"])


def delete_later(bot, chat_id: int, message_id: int, delay: float = 4.0) -> None:
    """Удаляет сообщение через delay секунд в фоне, чтобы не засорять историю чата."""
    async def _worker():
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    asyncio.create_task(_worker())

INBOX: Path = Path(os.environ.get("NAVISAN_INBOX", "/inbox"))
STAGING: Path = Path(os.environ.get("NAVISAN_STAGING", "/staging"))
LIBRARY: Path = Path(os.environ.get("NAVISAN_LIBRARY", "/library"))
DB_PATH: str = os.environ.get("NAVISAN_DB", "navidrome.db")
DEFAULT_ACTION: str = os.environ.get("NAVISAN_DEFAULT_ACTION", "manual").lower()


# ── Состояние сессии (State Machine) ──────────────────────────────────────────

class SessionState(Enum):
    """Этапы обработки одного альбома."""
    COVER_PENDING = auto()   # слайдшоу показано, ждём выбора
    PROCESSING = auto()      # pipeline работает
    DONE = auto()            # завершено


PAGE_SIZE = 4


@dataclass
class AlbumSession:
    """Контекст обработки одного альбома."""
    state: SessionState
    folder: Path                        # папка в Inbox или Library
    staging_folder: Path | None = None  # копия в Staging
    artist: str = ""
    album: str = ""
    n_tracks: int = 0
    candidates: list[CoverCandidate] = field(default_factory=list)
    chosen_cover: CoverCandidate | None = None
    picker_message_id: int | None = None # ID сообщения со слайдшоу
    is_recover: bool = False            # True для смены обложки в Library через /cover
    page: int = 0


# Один активный сеанс на пользователя
_sessions: dict[int, AlbumSession] = {}


# ── Авторизация ───────────────────────────────────────────────────────────────

def _owner_only(func):
    """Декоратор — молча игнорирует любые запросы не от владельца."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id != OWNER_ID:
            return
        return await func(update, ctx)
    return wrapper


# ── Нативный Telegram Slideshow (Rich Message) ────────────────────────────────

async def _send_native_slideshow(
    app: Application,
    session: AlbumSession,
) -> int:
    """Отправляет нативный Telegram Slideshow через метод sendRichMessage."""
    total_candidates = len(session.candidates)
    total_pages = max(1, (total_candidates + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = session.page % total_pages
    start_idx = current_page * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_candidates)
    display = session.candidates[start_idx:end_idx]

    slideshow_blocks = []
    for i, c in enumerate(display):
        global_idx = start_idx + i
        caption_parts = [f"#{global_idx + 1} · [{c.source}]"]
        if c.resolution_str:
            caption_parts.append(f"· {c.resolution_str}")
        if c.date:
            caption_parts.append(f"({c.date})")
        if c.tracks:
            caption_parts.append(f"· {c.tracks} треков")
        slide_caption = " ".join(caption_parts)

        slideshow_blocks.append({
            "type": "photo",
            "photo": {
                "type": "photo",
                "media": c.small_url,
            },
            "caption": {
                "text": slide_caption,
            },
        })

    # Кнопки с номерами, названиями источников и разрешением (по 2 в ряд)
    buttons = []
    for i, c in enumerate(display):
        global_idx = start_idx + i
        res_part = f" ({c.resolution_str})" if c.resolution_str else ""
        buttons.append(
            InlineKeyboardButton(f"{global_idx + 1}. {c.source}{res_part}", callback_data=f"cover:pick:{global_idx}")
        )

    keyboard = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

    if total_pages > 1:
        keyboard.append([
            InlineKeyboardButton(
                f"🔄 Другие варианты ({current_page + 1}/{total_pages})",
                callback_data="cover:next_page",
            )
        ])

    skip_text = "⏭️ Оставить текущую" if session.is_recover else "⏭️ Без обложки"
    keyboard.append([
        InlineKeyboardButton(skip_text, callback_data="cover:skip"),
        InlineKeyboardButton("❌ Отмена", callback_data="cover:cancel"),
    ])

    page_info = f" (варианты {current_page + 1}/{total_pages})" if total_pages > 1 else ""
    header_text = (
        f"🖼️ Смена обложки: {session.artist} — {session.album}{page_info}"
        if session.is_recover
        else f"📥 Новый релиз: {session.artist} — {session.album} ({session.n_tracks} треков){page_info}"
    )

    payload = {
        "chat_id": OWNER_ID,
        "rich_message": {
            "blocks": [
                {
                    "type": "paragraph",
                    "text": header_text,
                },
                {
                    "type": "slideshow",
                    "blocks": slideshow_blocks,
                },
            ]
        },
        "reply_markup": {
            "inline_keyboard": [
                [{"text": b.text, "callback_data": b.callback_data} for b in row]
                for row in keyboard
            ]
        },
    }

    try:
        res = await app.bot._do_post("sendRichMessage", payload)
        return res.get("message_id")
    except Exception as exc:
        err_str = str(exc).lower()
        if "wrong type of the web page content" in err_str or "bad request" in err_str:
            log.warning("sendRichMessage отклонен Telegram ('%s'). Выполняем автоисправление кандидатов страницы...", exc)
            # 1. Пробуем заменить small_url на big_url у тех кандидатов, где они различаются
            candidates_list = list(session.candidates)
            fixed_any = False
            for i, c in enumerate(display):
                if c.small_url != c.big_url:
                    g_idx = start_idx + i
                    candidates_list[g_idx] = replace(c, small_url=c.big_url)
                    fixed_any = True

            if fixed_any:
                session.candidates = candidates_list
                log.info("Повторная попытка sendRichMessage с big_url...")
                try:
                    return await _send_native_slideshow(app, session)
                except Exception:
                    pass

            # 2. Если замена не помогла, исключаем сбойных кандидатов из session.candidates
            valid_cands = []
            for c in session.candidates:
                if c in display:
                    try:
                        r = requests.get(c.small_url, timeout=2.0, headers={"User-Agent": "Mozilla/5.0"})
                        if r.status_code == 200 and "image" in r.headers.get("content-type", "").lower():
                            valid_cands.append(c)
                        else:
                            log.warning("Исключена сбойная обложка [%s]: %s (status=%d, ctype=%s)", c.source, c.small_url, r.status_code, r.headers.get("content-type", ""))
                    except Exception as test_exc:
                        log.warning("Исключена сбойная обложка [%s]: %s (ошибка: %s)", c.source, c.small_url, test_exc)
                else:
                    valid_cands.append(c)

            if len(valid_cands) < len(session.candidates) and valid_cands:
                session.candidates = valid_cands
                log.info("Исключены сбойные кандидаты, осталось %d. Повторная отправка...", len(valid_cands))
                return await _send_native_slideshow(app, session)
        raise


# ── Основной flow: обнаружение релиза ─────────────────────────────────────────

async def _on_new_release(folder: Path, app: Application, is_recover: bool = False) -> None:
    """Запускает поиск обложек и открывает нативный слайдшоу."""
    if not is_recover and count_flacs(folder) == 1:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: enrich_single_track_release(folder))
        except Exception as e:
            log.warning("Ошибка enrich_single_track_release для %s: %s", folder.name, e)

    artist, album = album_info(folder)
    n_tracks = count_flacs(folder)

    log.info("Релиз для обработки: %s — %s (is_recover=%s)", artist, album, is_recover)

    existing_note = ""
    if not is_recover and LIBRARY.exists() and artist and album:
        from renamer import FileSystemManager
        probe_folder = folder.parent / FileSystemManager().clean_name(f"{artist} - {album}")
        existing_dest = find_existing_library_album(probe_folder, LIBRARY)
        if existing_dest:
            existing_note = f"\nℹ️ *Альбом уже есть в библиотеке:* `{existing_dest.name}`\n_Новые треки будут объединены с ним без создания дубликатов._\n"

    header = f"🖼️ *Смена обложки в {LIBRARY.name}*" if is_recover else f"📥 *Новый релиз в {INBOX.name}*"
    status_msg = await app.bot.send_message(
        chat_id=OWNER_ID,
        text=(
            f"{header}\n\n"
            f"🎵 {artist} — {album}\n"
            f"📂 `{folder.name}` ({n_tracks} треков){existing_note}\n\n"
            f"🔍 Ищу обложки на стримингах…"
        ),
        parse_mode="Markdown",
    )

    loop = asyncio.get_running_loop()
    candidates: list[CoverCandidate] = await loop.run_in_executor(
        None, lambda: search_covers(artist, album) if (artist and album) else []
    )

    session = AlbumSession(
        state=SessionState.COVER_PENDING,
        folder=folder,
        artist=artist,
        album=album,
        n_tracks=n_tracks,
        candidates=candidates,
        is_recover=is_recover,
        page=0,
    )
    _sessions[OWNER_ID] = session

    # Удаляем временное сообщение "Ищу обложки…", чтобы не засорять чат
    try:
        await app.bot.delete_message(chat_id=OWNER_ID, message_id=status_msg.message_id)
    except Exception:
        pass

    # Автоматический режим (если в .env задано NAVISAN_DEFAULT_ACTION=auto)
    if DEFAULT_ACTION == "auto" and not is_recover:
        chosen = candidates[0] if candidates else None
        session.chosen_cover = chosen
        session.state = SessionState.PROCESSING
        res_info = f" ({chosen.resolution_str})" if (chosen and chosen.resolution_str) else ""
        cover_info = f"обложка #{1} [{chosen.source}]{res_info}" if chosen else "без обложки"
        proc_msg = await app.bot.send_message(
            chat_id=OWNER_ID,
            text=f"⚙️ *Автоматическая обработка релиза ({cover_info}):*\n🎵 {artist} — {album}",
            parse_mode="Markdown",
        )
        await _execute_pipeline_and_report(proc_msg, app, session)
        return

    if candidates:
        try:
            msg_id = await _send_native_slideshow(app, session)
            session.picker_message_id = msg_id
            return
        except Exception as e:
            log.warning("Не удалось отправить rich message slideshow: %s. Падаем на кнопки.", e)

    # Если обложек не найдено или произошла ошибка
    keyboard = [
        [InlineKeyboardButton("⏭️ Продолжить без обложки", callback_data="cover:skip")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cover:cancel")],
    ]
    no_covers_msg = await app.bot.send_message(
        chat_id=OWNER_ID,
        text=f"😕 Обложки для *{artist} — {album}* не найдены на стримингах.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    session.picker_message_id = no_covers_msg.message_id


# ── Обработчик выбора обложки ─────────────────────────────────────────────────

async def on_cover_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка выбора кнопки под слайдшоу."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_ID:
        return

    session = _sessions.get(OWNER_ID)
    if not session or session.state != SessionState.COVER_PENDING:
        await query.answer("⚠️ Сессия устарела. Запусти поиск заново.", show_alert=True)
        return

    _, action, *args = query.data.split(":")

    # 1. Переключение на следующую пачку обложек (Round-Robin)
    if action == "next_page":
        old_msg_id = query.message.message_id if query.message else session.picker_message_id
        session.page += 1
        try:
            msg_id = await _send_native_slideshow(ctx.application, session)
            session.picker_message_id = msg_id
            # Удаляем старое сообщение со слайдшоу только ПОСЛЕ успешного показа новой страницы
            if old_msg_id:
                try:
                    await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=old_msg_id)
                except Exception:
                    pass
        except Exception as e:
            session.page -= 1
            log.error("Ошибка при показе следующей страницы обложек: %s", e)
            await query.answer(f"⚠️ Не удалось загрузить страницу: {e}", show_alert=True)
        return

    # Для остальных действий (cancel, skip, pick) удаляем сообщение со слайдшоу сразу
    if query.message:
        try:
            await query.delete_message()
        except Exception:
            pass
    if session.picker_message_id and (not query.message or query.message.message_id != session.picker_message_id):
        try:
            await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=session.picker_message_id)
        except Exception:
            pass

    # 2. Отмена
    if action == "cancel":
        session.state = SessionState.DONE
        msg = "Обработка отменена." if not session.is_recover else "Смена обложки отменена."
        await query.answer(f"❌ {msg}")
        return

    # 3. Пропуск обложки
    if action == "skip":
        if session.is_recover:
            session.state = SessionState.DONE
            await query.answer("Обложка оставлена без изменений.")
            return

        session.chosen_cover = None
        session.state = SessionState.PROCESSING
        status_msg = await ctx.bot.send_message(chat_id=OWNER_ID, text="⚙️ Обрабатываю релиз без обложки…")
        await _execute_pipeline_and_report(status_msg, ctx, session)
        return

    # 4. Выбор обложки
    if action == "pick":
        idx = int(args[0])
        if not (0 <= idx < len(session.candidates)):
            await query.answer("❌ Неверный номер обложки.", show_alert=True)
            return

        chosen = session.candidates[idx]
        session.chosen_cover = chosen
        session.state = SessionState.PROCESSING

        # Режим /cover (замена обложки в Library):
        if session.is_recover:
            res_note = f" ({chosen.resolution_str})" if chosen.resolution_str else ""
            status_msg = await ctx.bot.send_message(
                chat_id=OWNER_ID,
                text=f"⏳ Скачиваю обложку #{idx + 1} [{chosen.source}]{res_note} и вшиваю в треки…"
            )
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, lambda: update_album_cover(session.folder, chosen)
            )
            session.state = SessionState.DONE
            final_text = (
                f"✅ *Обложка успешно обновлена!*\n\n"
                f"🎵 {session.artist} — {session.album}\n"
                f"📂 `{session.folder.name}`\n"
                f"{res}"
            )
            await ctx.bot.edit_message_text(
                chat_id=OWNER_ID,
                message_id=status_msg.message_id,
                text=final_text,
                parse_mode="Markdown",
            )
            delete_later(ctx.bot, OWNER_ID, status_msg.message_id, delay=5.0)
            return

        # Обычный режим (Inbox -> Staging -> Library):
        status_msg = await ctx.bot.send_message(
            chat_id=OWNER_ID,
            text=f"⏳ Выбрана обложка #{idx + 1} [{chosen.source}]\n⚙️ Обрабатываю релиз…"
        )
        await _execute_pipeline_and_report(status_msg, ctx, session)


async def _execute_pipeline_and_report(status_msg, ctx_or_app, session: AlbumSession) -> None:
    """Выполняет пайплайн и заменяет статусное сообщение на финальный аккуратный отчёт."""
    bot = ctx_or_app.bot if hasattr(ctx_or_app, "bot") else ctx_or_app
    bot_data = getattr(ctx_or_app, "bot_data", None)
    if bot_data is None and hasattr(ctx_or_app, "application"):
        bot_data = ctx_or_app.application.bot_data

    loop = asyncio.get_running_loop()
    try:
        report_lines, library_folder = await loop.run_in_executor(
            None, lambda: _run_full_pipeline(session)
        )
    except Exception as e:
        log.exception("Pipeline error for %s", session.folder.name)
        session.state = SessionState.DONE
        error_text = f"❌ Ошибка обработки: `{e}`"
        try:
            await bot.edit_message_text(
                chat_id=OWNER_ID,
                message_id=status_msg.message_id,
                text=error_text,
                parse_mode="Markdown",
            )
        except Exception:
            await bot.send_message(chat_id=OWNER_ID, text=error_text, parse_mode="Markdown")
        return

    session.state = SessionState.DONE
    artist, album = album_info(library_folder)
    header_artist = f"💿 *{artist} — {album}*\n👤 *Исполнитель релиза:* `{artist}`\n\n" if (artist and album) else ""
    report = "\n".join(report_lines)
    is_merged = any("Объединено с альбомом" in line for line in report_lines)
    status_title = "Альбом обновлён в библиотеке (добавлены треки)!" if is_merged else "Альбом добавлен в библиотеку!"
    final_text = f"✅ *{status_title}*\n\n{header_artist}{report}"

    if bot_data is not None:
        bot_data[f"release_report_{library_folder.name}"] = final_text

    keyboard = [
        [
            InlineKeyboardButton("📝 Настроить тексты", callback_data=f"lyrics:album:{library_folder.name}"),
            InlineKeyboardButton("👤 Настроить артистов", callback_data=f"artist:album:{library_folder.name}"),
        ],
        [
            InlineKeyboardButton("❌ Закрыть", callback_data="report:close"),
        ],
    ]

    try:
        await bot.edit_message_text(
            chat_id=OWNER_ID,
            message_id=status_msg.message_id,
            text=final_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        await bot.send_message(
            chat_id=OWNER_ID,
            text=final_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def on_report_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик действий финального отчёта релиза."""
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != OWNER_ID:
        return

    if query.data == "report:close":
        try:
            await query.delete_message()
        except Exception:
            pass
        return

    if query.data.startswith("report:show:"):
        album_name = query.data[len("report:show:"):]
        album_dir = LIBRARY / album_name
        if not album_dir.exists():
            matches = [d for d in LIBRARY.iterdir() if d.is_dir() and album_name.lower() in d.name.lower()]
            if matches:
                album_dir = matches[0]
                album_name = album_dir.name

        report_text = ctx.bot_data.get(f"release_report_{album_name}")
        if not report_text:
            artist, album = album_info(album_dir) if album_dir.exists() else ("", "")
            header = f"💿 *{artist} — {album}*\n👤 *Исполнитель релиза:* `{artist}`\n\n" if (artist and album) else ""
            report_text = f"✅ *Альбом в библиотеке!*\n\n{header}📂 `{album_name}`"
        else:
            if album_dir.exists():
                curr_artist, curr_album = album_info(album_dir)
                if curr_artist and curr_album:
                    new_header = f"💿 *{curr_artist} — {curr_album}*\n👤 *Исполнитель релиза:* `{curr_artist}`"
                    import re
                    report_text = re.sub(r"💿 \*.*?\*\n👤 \*Исполнитель релиза:\* `.*?`", new_header, report_text)
                    ctx.bot_data[f"release_report_{album_name}"] = report_text

        keyboard = [
            [
                InlineKeyboardButton("📝 Настроить тексты", callback_data=f"lyrics:album:{album_name}"),
                InlineKeyboardButton("👤 Настроить артистов", callback_data=f"artist:album:{album_name}"),
            ],
            [
                InlineKeyboardButton("❌ Закрыть", callback_data="report:close"),
            ],
        ]
        await _safe_edit_message(query, report_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return



def _run_full_pipeline(session: AlbumSession) -> tuple[list[str], Path]:
    """Синхронный полный pipeline: Inbox → Staging → обработка → Library."""
    report: list[str] = []

    # 1. Копируем в Staging
    staging_folder = copy_to_staging(session.folder, STAGING)
    session.staging_folder = staging_folder

    # 2. Скачиваем обложку (если выбрана)
    if session.chosen_cover:
        cover_line = apply_cover(staging_folder, session.chosen_cover)
        report.append(cover_line)

    # 3. Полный pipeline: теги → переименование → тексты
    pipeline_lines, staging_folder = apply_pipeline(
        staging_folder,
        db_path=DB_PATH,
    )
    report.extend(pipeline_lines)
    session.staging_folder = staging_folder

    # 4. Переносим в Library (со слиянием при наличии альбома)
    library_folder, merge_info = move_to_library(
        staging_folder,
        LIBRARY,
        has_chosen_cover=(session.chosen_cover is not None),
    )
    if merge_info.get("merged"):
        added_count = len(merge_info.get("added_tracks", []))
        updated_count = len(merge_info.get("updated_tracks", []))
        details = []
        if added_count:
            details.append(f"добавлено: {added_count}")
        if updated_count:
            details.append(f"обновлено: {updated_count}")
        details_str = f" ({', '.join(details)})" if details else ""
        report.append(f"📦 Объединено с альбомом в библиотеке: `{library_folder.name}`{details_str}")
        report.append(f"💿 Всего треков в альбоме теперь: *{merge_info.get('total_flacs', 0)}*")
        if merge_info.get("rg_ok"):
            report.append("🔊 ReplayGain: обновлён для всего альбома")
    else:
        report.append(f"📦 Перенесено в: `{library_folder.name}`")

    # 5. Чистим Inbox
    cleanup_inbox(session.folder)
    report.append(f"🗑️ {INBOX.name} очищен")

    return report, library_folder


# ── Команды ───────────────────────────────────────────────────────────────────

@_owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Бот автоматически следит за папкой `{INBOX.name}`.\n"
        "Когда появится новый релиз — я пришлю варианты обложек "
        "и перенесу альбом без лишнего мусора.\n\n"
        "*Команды:*\n"
        f"• `/upgrade` — пакетная обработка всей папки `{LIBRARY.name}` разом\n"
        f"• `/scan` — сканировать `{INBOX.name}` и выбрать релиз вручную\n"
        f"• `/cover [поиск]` — сменить обложку для альбома в `{LIBRARY.name}`\n"
        "• `/lyrics [поиск]` — превью и настройка текстов песен (.lrc)\n"
        "• `/artist [поиск]` — проверить и настроить артистов релиза\n"
        f"• `/process <папка>` — принудительно обработать релиз из `{INBOX.name}`\n"
        "• `/status` — статус текущей сессии\n"
        "• `/cancel` — отменить текущий ввод текста",
        parse_mode="Markdown",
    )


@_owner_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    session = _sessions.get(OWNER_ID)
    if not session:
        await update.message.reply_text(f"Нет активных сессий. Жду новых релизов в {INBOX.name}.")
        return

    state_emoji = {
        SessionState.COVER_PENDING: "🖼️ Ожидание выбора обложки",
        SessionState.PROCESSING: "⚙️ Обработка",
        SessionState.DONE: "✅ Завершено",
    }
    cover_res = f" ({session.chosen_cover.resolution_str})" if (session.chosen_cover and session.chosen_cover.resolution_str) else ""
    cover_info = f"{session.chosen_cover.source}{cover_res}" if session.chosen_cover else "не выбрана"

    await update.message.reply_text(
        f"📂 Папка: `{session.folder.name}`\n"
        f"🎵 {session.artist} — {session.album}\n"
        f"📊 Статус: {state_emoji.get(session.state, '❓')}\n"
        f"🖼️ Обложка: {cover_info}\n"
        f"🔢 Кандидатов: {len(session.candidates)}",
        parse_mode="Markdown",
    )


@_owner_only
async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Принудительное сканирование Inbox — для папок, которые уже лежали в папке."""
    if not INBOX.exists():
        msg = await update.message.reply_text(f"❌ Папка {INBOX.name} не найдена.", parse_mode="Markdown")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    folders = sorted([
        d for d in INBOX.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
    ])

    if not folders:
        msg = await update.message.reply_text(f"📭 В {INBOX.name} нет папок с FLAC-файлами.")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    lines = [f"📥 *Папки в {INBOX.name} ({len(folders)}):*\n"]
    buttons = []
    for i, d in enumerate(folders):
        artist, album = album_info(d)
        n = count_flacs(d)
        display = f"{artist} — {album}" if album else d.name
        lines.append(f"{i + 1}. `{d.name}` — {display} ({n} треков)")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"scan:{i}"))

    lines.append("\n💬 _Напиши имя папки в чат для быстрого поиска._")
    ctx.bot_data["scan_folders"] = folders
    ctx.bot_data["scan_cmd_msg_id"] = update.message.message_id
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="scan:close")])

    sent_msg = await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    ctx.bot_data[f"active_menu_{OWNER_ID}"] = {"cmd": "scan", "msg_id": sent_msg.message_id}


async def on_scan_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик выбора папки из /scan."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_ID:
        return

    # Закрытие меню выбора папки
    if query.data == "scan:close":
        ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)
        try:
            await query.delete_message()
        except Exception:
            pass
        cmd_id = ctx.bot_data.pop("scan_cmd_msg_id", None)
        if cmd_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=cmd_id)
            except Exception:
                pass
        return

    ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)

    folders = ctx.bot_data.get("scan_folders", [])
    _, payload = query.data.split(":", 1)
    idx = int(payload)

    if idx >= len(folders):
        await query.answer("❌ Некорректный номер папки", show_alert=True)
        return

    folder = folders[idx]

    # Удаляем меню scan и исходную команду /scan пользователя
    try:
        await query.delete_message()
    except Exception:
        pass
    cmd_id = ctx.bot_data.pop("scan_cmd_msg_id", None)
    if cmd_id:
        try:
            await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=cmd_id)
        except Exception:
            pass

    await _on_new_release(folder, ctx.application)


@_owner_only
async def cmd_cover(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Смена обложки для уже существующего альбома в Library."""
    if not LIBRARY.exists():
        msg = await update.message.reply_text(f"❌ Папка {LIBRARY.name} не найдена.", parse_mode="Markdown")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    query_str = " ".join(ctx.args).strip() if ctx.args else ""

    all_albums = sorted([
        d for d in LIBRARY.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
    ])

    if not all_albums:
        msg = await update.message.reply_text(f"📭 В {LIBRARY.name} пока нет альбомов с FLAC-файлами.")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    if query_str:
        matches = [d for d in all_albums if query_str.lower() in d.name.lower()]
    else:
        matches = all_albums[-10:]

    if not matches:
        msg = await update.message.reply_text(f"❌ Ничего не найдено по запросу: `{query_str}`", parse_mode="Markdown")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    # Если ровно одно совпадение — сразу открываем слайдшоу!
    if len(matches) == 1 and query_str:
        try:
            await update.message.delete()
        except Exception:
            pass
        await _on_new_release(matches[0], ctx.application, is_recover=True)
        return

    # Если несколько — даём список с кнопками
    lines = [f"🖼️ *Выбери альбом для смены обложки ({len(matches)}):*\n"]
    buttons = []
    for i, d in enumerate(matches):
        artist, album = album_info(d)
        display = f"{artist} — {album}" if album else d.name
        lines.append(f"{i + 1}. `{d.name}` — {display}")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"recover:{i}"))

    lines.append("\n💬 _Напиши название релиза в чат для быстрого поиска._")
    ctx.bot_data["recover_folders"] = matches
    ctx.bot_data["cover_cmd_msg_id"] = update.message.message_id
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="recover:close")])

    sent_msg = await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    ctx.bot_data[f"active_menu_{OWNER_ID}"] = {"cmd": "cover", "msg_id": sent_msg.message_id}


async def on_recover_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Выбор альбома для /cover."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_ID:
        return

    # Закрытие меню
    if query.data == "recover:close":
        ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)
        try:
            await query.delete_message()
        except Exception:
            pass
        cmd_id = ctx.bot_data.pop("cover_cmd_msg_id", None)
        if cmd_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=cmd_id)
            except Exception:
                pass
        return

    ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)

    folders = ctx.bot_data.get("recover_folders", [])
    _, payload = query.data.split(":", 1)
    idx = int(payload)

    if idx >= len(folders):
        await query.answer("❌ Некорректный номер альбома", show_alert=True)
        return

    folder = folders[idx]
    try:
        await query.delete_message()
    except Exception:
        pass
    cmd_id = ctx.bot_data.pop("cover_cmd_msg_id", None)
    if cmd_id:
        try:
            await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=cmd_id)
        except Exception:
            pass

    await _on_new_release(folder, ctx.application, is_recover=True)


@_owner_only
async def cmd_process(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Ручной запуск обработки для указанной папки из Inbox."""
    if not ctx.args:
        msg = await update.message.reply_text(
            f"Укажи папку в `{INBOX.name}`:\n`/process <папка>`\nИли выбери через `/scan`",
            parse_mode="Markdown",
        )
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=5.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=5.0)
        return

    target_str = " ".join(ctx.args).strip()
    folder = Path(target_str)

    # Если передан относительный путь или только имя папки в Inbox
    if not folder.is_absolute() or not folder.exists():
        inbox_candidate = INBOX / target_str
        if inbox_candidate.exists():
            folder = inbox_candidate

    if not folder.exists() or not folder.is_dir():
        msg = await update.message.reply_text(f"❌ Папка не найдена: `{Path(target_str).name}`", parse_mode="Markdown")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=5.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=5.0)
        return

    try:
        await update.message.delete()
    except Exception:
        pass

    await _on_new_release(folder, ctx.application)


@_owner_only
async def cmd_upgrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /upgrade — пакетная идеализация всей библиотеки разом."""
    if not LIBRARY.exists():
        msg = await update.message.reply_text(f"❌ Папка {LIBRARY.name} не найдена.", parse_mode="Markdown")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    albums = sorted([
        d for d in LIBRARY.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.rglob("*.flac") if f.is_file())
    ])

    if not albums:
        msg = await update.message.reply_text(f"📭 В {LIBRARY.name} пока нет альбомов с FLAC-файлами.")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    args = ctx.args or []
    query_parts = [a for a in args if a.lower() != "force"]
    query_str = " ".join(query_parts).strip()

    if query_str:
        matches = [d for d in albums if query_str.lower() in d.name.lower()]
        if not matches:
            msg = await update.message.reply_text(f"❌ В {LIBRARY.name} не найдено альбомов по запросу: `{query_str}`", parse_mode="Markdown")
            delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
            delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
            return
        albums = matches
        ctx.bot_data["upgrade_target_albums"] = [str(a) for a in albums]
    else:
        ctx.bot_data.pop("upgrade_target_albums", None)

    try:
        await update.message.delete()
    except Exception:
        pass

    target_desc = f"По запросу: `{query_str}` ({len(albums)} альбомов)" if query_str else f"Всей библиотеки ({len(albums)} альбомов)"
    text = (
        f"📁 Каталог: `{LIBRARY.name}`\n"
        f"💿 Режим: *{target_desc}*\n\n"
        "*Выбери режим идеализации:*\n\n"
        "• ▶️ *Обычный (мягкий)* — быстро заполнит только пропуски: найдет недостающие обложки, недостающие тексты и проставит ReplayGain. Существующие файлы не перезаписывает.\n\n"
        "• ⚡ *Force (полная пересборка)* — с нуля очистит теги от мусора (извлечёт фиты из названий), перекачает Hi-Res обложки, обновит тексты (с защитой караоке от даунгрейда) и заново рассчитает ReplayGain.\n\n"
        "💬 _Напиши название альбома в чат для фильтрации апгрейда._"
    )

    keyboard = [
        [InlineKeyboardButton("▶️ Обычный апгрейд", callback_data="upgrade:start:normal")],
        [InlineKeyboardButton("⚡ Полная пересборка (Force)", callback_data="upgrade:start:force")],
        [InlineKeyboardButton("❌ Отмена", callback_data="upgrade:close")],
    ]

    sent_msg = await ctx.bot.send_message(
        chat_id=OWNER_ID,
        text=text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    ctx.bot_data[f"active_menu_{OWNER_ID}"] = {"cmd": "upgrade", "msg_id": sent_msg.message_id}


async def on_upgrade_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик интерактивной идеализации библиотеки."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id != OWNER_ID:
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "close":
        ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)
        try:
            await query.delete_message()
        except Exception:
            pass
        return

    if action == "start":
        ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)

    if action == "stop":
        ctx.bot_data["upgrade_cancelled"] = True
        log.info("Остановка идеализации библиотеки запрошена пользователем (upgrade:stop)")
        await _safe_edit_message(
            query,
            "⏹️ *Останавливаю процесс...*\n\n_Пожалуйста, подожди завершения текущей операции..._",
            reply_markup=None,
        )
        await _safe_answer_query(query, "Останавливаю процесс…")
        return

    if action == "start":
        mode = parts[2] if len(parts) > 2 else "normal"
        is_force = (mode == "force")

        if ctx.bot_data.get("upgrade_running"):
            await query.answer("Идеализация уже выполняется!", show_alert=True)
            return

        ctx.bot_data["upgrade_running"] = True
        ctx.bot_data["upgrade_cancelled"] = False

        last_edit_time = 0.0
        loop = asyncio.get_running_loop()

        mode_badge = "⚡ FORCE" if is_force else "▶️ ОБЫЧНЫЙ"

        def _on_progress(cur: int, tot: int, alb_name: str, res: dict):
            if ctx.bot_data.get("upgrade_cancelled"):
                return
            nonlocal last_edit_time
            now = time.time()
            # Промежуточный прогресс отправляем только для cur < tot, чтобы не конфликтовать с финальным сообщением
            if cur < tot and (now - last_edit_time > 2.0):
                last_edit_time = now
                pct = int((cur / tot) * 100)
                rg_str = " | 🔊 RG: +" if res.get("replaygain_added") else ""
                txt = (
                    f"⏳ *Идеализация библиотеки [{mode_badge}]: {cur}/{tot} ({pct}%)*\n\n"
                    f"💿 Текущий: *{alb_name}*\n"
                    f"📝 Тексты: +{res.get('lyrics_added', 0)} | 🖼️ Обложка: {'+' if res.get('cover_added') else 'OK'}{rg_str}\n\n"
                    f"_Пожалуйста, подожди завершения…_"
                )
                kb = [[InlineKeyboardButton("⏹️ Остановить", callback_data="upgrade:stop")]]
                try:
                    asyncio.run_coroutine_threadsafe(
                        _safe_edit_message(query, txt, reply_markup=InlineKeyboardMarkup(kb)),
                        loop,
                    )
                except Exception:
                    pass

        def _is_cancelled() -> bool:
            return bool(ctx.bot_data.get("upgrade_cancelled", False))

        init_text = f"⏳ *Идеализация библиотеки [{mode_badge}] запущена…*\n\nАнализирую релизы…"
        init_kb = [[InlineKeyboardButton("⏹️ Остановить", callback_data="upgrade:stop")]]
        await _safe_edit_message(query, init_text, reply_markup=InlineKeyboardMarkup(init_kb))

        target_albums_list = None
        if "upgrade_target_albums" in ctx.bot_data:
            target_albums_list = [Path(p) for p in ctx.bot_data["upgrade_target_albums"]]

        try:
            summary = await loop.run_in_executor(
                None,
                lambda: upgrade_library(
                    LIBRARY,
                    db_path=DB_PATH,
                    progress_callback=_on_progress,
                    cancel_flag=_is_cancelled,
                    force=is_force,
                    target_albums=target_albums_list,
                ),
            )
        finally:
            ctx.bot_data["upgrade_running"] = False
            ctx.bot_data.pop("upgrade_target_albums", None)

        await asyncio.sleep(0.5)

        stopped_str = " (прервано пользователем)" if ctx.bot_data.get("upgrade_cancelled") else ""
        rg_added_str = f"\n🔊 ReplayGain рассчитан: *{summary.get('replaygain_added', 0)}* альбомов" if summary.get("replaygain_added") else ""
        exp_added_str = f"\n🔞 Explicit (ITUNESADVISORY): *{summary.get('explicit_added', 0)}* треков" if summary.get("explicit_added") else ""
        lbl_added_str = f"\n🏷️ Лейблы (LABEL): *{summary.get('labels_added', 0)}* альбомов" if summary.get("labels_added") else ""
        final_text = (
            f"✅ *Идеализация библиотеки [{mode_badge}] завершена{stopped_str}!* 🎉\n\n"
            f"💿 Обработано альбомов: *{summary['processed']}/{summary['total']}*\n"
            f"🖼️ Добавлено/обновлено обложек: *{summary['covers_added']}*\n"
            f"📝 Загружено текстов (.lrc): *{summary['lyrics_added']}*"
            f"{rg_added_str}"
            f"{exp_added_str}"
            f"{lbl_added_str}"
        )
        final_kb = [[InlineKeyboardButton("❌ Закрыть", callback_data="upgrade:close")]]
        await _safe_edit_message(query, final_text, reply_markup=InlineKeyboardMarkup(final_kb))


async def _safe_edit_message(query, text: str, reply_markup=None, parse_mode="Markdown") -> None:
    """Редактирует сообщение, безопасно игнорируя 'Message is not modified' и обрабатывая ограничения Telegram."""
    from telegram.error import RetryAfter
    try:
        await query.edit_message_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            log.warning("BadRequest при редактировании сообщения: %s", e)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        try:
            await query.edit_message_text(text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception as e2:
            log.warning("Ошибка повторного редактирования сообщения: %s", e2)
    except TelegramError as e:
        log.warning("TelegramError при редактировании сообщения: %s", e)


async def _safe_answer_query(query, text: str | None = None, show_alert: bool = False) -> None:
    """Безопасно отвечает на callback_query, игнорируя истекший таймаут и сетевые сбои."""
    if not query:
        return
    try:
        if text:
            await query.answer(text=text, show_alert=show_alert)
        else:
            await query.answer()
    except BadRequest as e:
        if "Query is too old" in str(e):
            log.debug("Игнорируем устаревший callback query: %s", e)
        else:
            log.warning("BadRequest при query.answer: %s", e)
    except TelegramError as e:
        log.warning("TelegramError при query.answer: %s", e)


# ── Настройка текстов песен (Lyrics UI) ───────────────────────────────────────

def _render_album_lyrics_tracklist(
    album_dir: Path, tracks: list[Path], from_report: bool = False
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует список треков альбома со статусами текстов и кнопками выбора."""
    artist, album = album_info(album_dir)
    title_str = f"{artist} — {album}" if album else album_dir.name

    lines = [f"📝 *Тексты альбома:*\n🎵 *{title_str}*\n"]
    buttons = []

    n_synced = 0
    n_plain = 0
    n_missing = 0

    for i, flac in enumerate(tracks):
        lrc = flac.with_suffix(".lrc")
        if lrc.exists():
            try:
                content = lrc.read_text(encoding="utf-8")
                if has_timestamps(content):
                    icon = "🎤"
                    n_synced += 1
                else:
                    icon = "📄"
                    n_plain += 1
            except Exception:
                icon = "📄"
                n_plain += 1
        else:
            icon = "❌"
            n_missing += 1

        lines.append(f"{i + 1}. {icon} `{flac.name}`")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"lyrics:tr:{i}"))

    lines.append(f"\n📊 Статус: 🎤 {n_synced} | 📄 {n_plain} | ❌ {n_missing}")
    lines.append("_Обозначения: 🎤 — синхронизированный текст, 📄 — обычный текст, ❌ — текста нет_\n")
    lines.append("Нажми номер трека для настройки или выбери пакетное действие:")

    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]

    # Пакетные кнопки альбома
    batch_row = []
    if n_missing > 0:
        batch_row.append(InlineKeyboardButton(f"🌐 Найти недостающие ({n_missing})", callback_data="lyrics:albmiss"))
    if batch_row:
        keyboard.append(batch_row)

    if from_report:
        keyboard.append([
            InlineKeyboardButton("⬅️ Назад к релизу", callback_data=f"report:show:{album_dir.name}")
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("📁 К альбомам", callback_data="lyrics:alb_list"),
            InlineKeyboardButton("❌ Закрыть", callback_data="lyrics:close"),
        ])

    return "\n".join(lines), keyboard


def _render_track_lyrics_card(
    track_path: Path, track_idx: int, from_report: bool = False
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует карточку трека с превью текста и кнопками смены источника."""
    rg_str = ""
    try:
        audio = FLAC(str(track_path))
        artist = audio.get("artist", audio.get("albumartist", [""]))[0]
        title = audio.get("title", [""])[0]
        rg_val = audio.get("replaygain_track_gain", [""])[0]
        if rg_val:
            rg_str = f" | 🔊 `{rg_val}`"
    except Exception:
        artist, title = "", ""

    if not title:
        title = track_path.stem

    lrc_path = track_path.with_suffix(".lrc")
    text_content = ""
    if lrc_path.exists():
        try:
            text_content = lrc_path.read_text(encoding="utf-8").strip()
        except Exception:
            text_content = ""

    if text_content:
        is_sync = has_timestamps(text_content)
        status = "🎤 *Синхронизированный текст*" if is_sync else "📄 *Обычный текст*"
        preview = preview_lyrics(text_content, max_lines=6)
    else:
        status = "❌ *Текст отсутствует*"
        preview = "—"

    header_artist = f"{artist} — " if artist else ""
    text = (
        f"🎵 *{header_artist}{title}*\n"
        f"📂 `{track_path.name}`\n"
        f"📊 Статус: {status}{rg_str}\n\n"
        f"📝 *Превью текста:*\n```\n{preview}\n```"
    )

    action_row = [
        InlineKeyboardButton("✏️ Свой текст", callback_data=f"lyrics:man:{track_idx}"),
    ]

    bottom_row = [InlineKeyboardButton("⬅️ К списку треков", callback_data="lyrics:back")]
    if not from_report:
        bottom_row.append(InlineKeyboardButton("❌ Закрыть", callback_data="lyrics:close"))

    keyboard = [
        [
            InlineKeyboardButton("🔍 LRCLIB", callback_data=f"lyrics:src:lrclib:{track_idx}"),
            InlineKeyboardButton("🔍 LRCMUX", callback_data=f"lyrics:src:lrcmux:{track_idx}"),
            InlineKeyboardButton("🔍 Genius", callback_data=f"lyrics:src:genius:{track_idx}"),
        ],
        [
            InlineKeyboardButton("🔍 SyncedLyrics", callback_data=f"lyrics:src:syncedlyrics:{track_idx}"),
        ],
        action_row,
        bottom_row,
    ]
    return text, keyboard


def _render_albums_menu(matches: list[Path]) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует меню выбора альбома для настройки текстов."""
    lines = [f"📝 *Выбери альбом для настройки текстов ({len(matches)}):*\n"]
    buttons = []
    for i, d in enumerate(matches):
        artist, album = album_info(d)
        display = f"{artist} — {album}" if album else d.name
        lines.append(f"{i + 1}. `{d.name}` — {display}")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"lyrics:alb:{i}"))

    lines.append("\n💬 _Напиши название релиза в чат для быстрого поиска._")
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    keyboard.append([InlineKeyboardButton("📊 Аудит текстов библиотеки", callback_data="lyrics:audit")])
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="lyrics:close")])
    return "\n".join(lines), keyboard


def _render_lyrics_audit_view(audit_data: dict) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует дашборд аудита текстов по всей библиотеке."""
    total = audit_data.get("total", 0)
    synced = audit_data.get("synced", [])
    plain = audit_data.get("plain", [])
    missing = audit_data.get("missing", [])
    albums_plain = audit_data.get("albums_plain", {})

    pct_synced = int((len(synced) / total) * 100) if total else 0
    pct_plain = int((len(plain) / total) * 100) if total else 0
    pct_missing = int((len(missing) / total) * 100) if total else 0

    lines = [
        "📊 *Аудит текстов библиотеки Navidrome:*\n",
        f"🎵 Всего FLAC-треков: *{total}*",
        f"🎤 Синхронизировано: *{len(synced)}* ({pct_synced}%)",
        f"📄 Обычный текст: *{len(plain)}* ({pct_plain}%)",
        f"❌ Без текста: *{len(missing)}* ({pct_missing}%)\n",
    ]

    if missing:
        lines.append(f"💡 Для треков без текста (*{len(missing)}*) можно запустить сетевой поиск.")
    elif plain:
        lines.append(f"ℹ️ В библиотеке *{len(plain)}* треков с обычным текстом (plain).")
    else:
        lines.append("🎉 Все треки библиотеки имеют тексты!")

    keyboard = []
    if missing:
        keyboard.append([
            InlineKeyboardButton(f"🌐 Найти недостающие тексты ({len(missing)})", callback_data="lyrics:libmiss")
        ])

    keyboard.append([
        InlineKeyboardButton("🔄 Обновить", callback_data="lyrics:audit"),
        InlineKeyboardButton("📁 К альбомам", callback_data="lyrics:alb_list"),
        InlineKeyboardButton("❌ Закрыть", callback_data="lyrics:close"),
    ])

    return "\n".join(lines), keyboard


@_owner_only
async def cmd_lyrics(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /lyrics [запрос|audit] — просмотр и настройка текстов песен в Library."""
    if not LIBRARY.exists():
        msg = await update.message.reply_text(f"❌ Папка {LIBRARY.name} не найдена.", parse_mode="Markdown")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    query_str = " ".join(ctx.args).strip() if ctx.args else ""

    if query_str.lower() in ("audit", "аудит", "stats", "статус"):
        try:
            await update.message.delete()
        except Exception:
            pass
        status_msg = await update.message.reply_text("⏳ Анализирую тексты в библиотеке...", parse_mode="Markdown")
        loop = asyncio.get_running_loop()
        audit_data = await loop.run_in_executor(None, lambda: audit_library_lyrics(LIBRARY))
        ctx.bot_data["lyrics_audit_data"] = audit_data
        text, keyboard = _render_lyrics_audit_view(audit_data)
        await status_msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    all_albums = sorted([
        d for d in LIBRARY.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
    ])

    if not all_albums:
        msg = await update.message.reply_text(f"📭 В {LIBRARY.name} пока нет альбомов с FLAC-файлами.")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    if query_str:
        matches = [d for d in all_albums if query_str.lower() in d.name.lower()]
    else:
        matches = all_albums[-10:]

    if not matches:
        msg = await update.message.reply_text(f"❌ Ничего не найдено по запросу: `{query_str}`", parse_mode="Markdown")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    # Если ровно один альбом совпал — открываем его список треков!
    if len(matches) == 1:
        try:
            await update.message.delete()
        except Exception:
            pass
        album_dir = matches[0]
        tracks = sorted(album_dir.rglob("*.flac"))
        ctx.bot_data["lyrics_tracks"] = tracks
        ctx.bot_data["lyrics_album_path"] = album_dir
        ctx.bot_data.pop("lyrics_from_report", None)
        text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks, from_report=False)
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    ctx.bot_data["lyrics_albums"] = matches
    ctx.bot_data["lyrics_cmd_msg_id"] = update.message.message_id
    ctx.bot_data.pop("lyrics_from_report", None)
    text, keyboard = _render_albums_menu(matches)
    sent_msg = await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    ctx.bot_data[f"active_menu_{OWNER_ID}"] = {"cmd": "lyrics", "msg_id": sent_msg.message_id}


# ── Настройка артистов (Artist UI) ───────────────────────────────────────────

def _update_album_references(ctx: ContextTypes.DEFAULT_TYPE, old_folder: Path, new_folder: Path) -> None:
    """Обновляет ссылки и кэши в bot_data при переименовании папки альбома."""
    if old_folder == new_folder:
        return
    ctx.bot_data["artist_album_path"] = new_folder
    ctx.bot_data["artist_tracks"] = sorted(new_folder.rglob("*.flac"))
    if "lyrics_albums" in ctx.bot_data:
        ctx.bot_data["lyrics_albums"] = [
            new_folder if d == old_folder else d for d in ctx.bot_data["lyrics_albums"]
        ]
    if ctx.bot_data.get("lyrics_album_path") == old_folder:
        ctx.bot_data["lyrics_album_path"] = new_folder
        ctx.bot_data["lyrics_tracks"] = sorted(new_folder.rglob("*.flac"))
    if ctx.bot_data.get("lyrics_from_report") == old_folder.name:
        ctx.bot_data["lyrics_from_report"] = new_folder.name
    if ctx.bot_data.get("artist_from_report") == old_folder.name:
        ctx.bot_data["artist_from_report"] = new_folder.name
    if f"release_report_{old_folder.name}" in ctx.bot_data:
        ctx.bot_data[f"release_report_{new_folder.name}"] = ctx.bot_data.pop(f"release_report_{old_folder.name}")


def _render_album_artists_view(
    album_dir: Path, from_report: bool = False
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует обзор артистов альбома и кнопки выбора треков."""
    info = get_album_artists_info(album_dir)
    album_display = info["album"] or album_dir.name

    lines = [
        "👤 *Артисты релиза:*",
        f"💿 *{album_display}*",
        f"📂 Папка: `{album_dir.name}`",
        f"🏷️ *ALBUMARTIST:* `{info['album_artist']}`\n",
        "🎵 *Треки и артисты:*",
    ]
    buttons = []
    for i, t in enumerate(info["tracks"]):
        num = t["track_num"] or str(i + 1)
        artists_str = ", ".join(t["artists"]) if t["artists"] else "—"
        lines.append(f"{i + 1}. `{num}. {t['title']}`\n   👤 _{artists_str}_")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"artist:tr:{i}"))

    lines.append("\n_Нажми номер трека, чтобы настроить его артистов:_")

    keyboard = [
        [
            InlineKeyboardButton("👥 Задать для всех треков", callback_data="artist:editall"),
            InlineKeyboardButton("🏷️ Изменить ALBUMARTIST", callback_data="artist:editaa"),
        ],
    ]
    for row_start in range(0, len(buttons), 5):
        keyboard.append(buttons[row_start : row_start + 5])

    if from_report:
        keyboard.append([InlineKeyboardButton("⬅️ Назад к релизу", callback_data=f"report:show:{album_dir.name}")])
    else:
        keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="artist:close")])
    return "\n".join(lines), keyboard


def _render_album_artists_all_prompt(
    album_dir: Path, from_report: bool = False
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует карточку ожидания ввода нового единого исполнителя для всех треков релиза."""
    info = get_album_artists_info(album_dir)
    album_display = info["album"] or album_dir.name
    current_aa = info["album_artist"]

    lines = [
        "👥 *Замена исполнителя во всех треках:*",
        f"💿 *{album_display}*",
        f"📂 Папка: `{album_dir.name}`",
        f"🏷️ Текущий ALBUMARTIST: `{current_aa}`\n",
        "Отправь верное имя исполнителя сообщением в чат.\n",
        "_Имя будет записано во все треки альбома, в тег ALBUMARTIST и обновит имя папки релиза._\n",
        "Для отмены нажми кнопку ниже или отправь /cancel.",
    ]
    back_row = [InlineKeyboardButton("⬅️ Назад к альбому", callback_data="artist:alb_back")]
    if not from_report:
        back_row.append(InlineKeyboardButton("❌ Закрыть", callback_data="artist:close"))
    keyboard = [back_row]
    return "\n".join(lines), keyboard


def _render_album_artist_edit_card(
    album_dir: Path, suggestions: list[str], from_report: bool = False
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует карточку редактирования ALBUMARTIST с динамическими подсказками из треков."""
    info = get_album_artists_info(album_dir)
    album_display = info["album"] or album_dir.name
    current_aa = info["album_artist"]

    example_hint = f" (например, `{suggestions[0]}`)" if suggestions else ""
    lines = [
        "🏷️ *Настройка ALBUMARTIST релиза:*",
        f"💿 *{album_display}*",
        f"📂 Папка: `{album_dir.name}`",
        f"🏷️ Текущий ALBUMARTIST: `{current_aa}`\n",
        "Выбери готовый вариант кнопкой ниже или нажми *«✍️ Ввести свой вариант»* (также можно просто отправить сообщение в чат)"
        f"{example_hint}.\n",
        "Для отмены команды отправь /cancel.",
    ]

    keyboard = []
    for idx, s in enumerate(suggestions):
        keyboard.append([
            InlineKeyboardButton(f"👤 {s}", callback_data=f"artist:setaa:{idx}")
        ])

    keyboard.append([
        InlineKeyboardButton("✍️ Ввести свой вариант", callback_data="artist:customaa")
    ])
    keyboard.append([
        InlineKeyboardButton("🗑️ Очистить ALBUMARTIST (удалить тег)", callback_data="artist:setaa:clear")
    ])

    back_row = [InlineKeyboardButton("⬅️ Назад к альбому", callback_data="artist:alb_back")]
    if not from_report:
        back_row.append(InlineKeyboardButton("❌ Закрыть", callback_data="artist:close"))
    keyboard.append(back_row)

    return "\n".join(lines), keyboard


def _render_album_artist_custom_prompt(
    album_dir: Path, from_report: bool = False
) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Формирует карточку ожидания ручного ввода ALBUMARTIST."""
    info = get_album_artists_info(album_dir)
    album_display = info["album"] or album_dir.name
    current_aa = info["album_artist"]
    suggestions = get_album_artist_suggestions(album_dir)
    example_hint = f" (например: `{suggestions[0]}`)" if suggestions else ""

    lines = [
        "✍️ *Ввод ALBUMARTIST вручную:*",
        f"💿 *{album_display}*",
        f"📂 Папка: `{album_dir.name}`",
        f"🏷️ Текущий ALBUMARTIST: `{current_aa}`\n",
        f"Отправь новое значение ALBUMARTIST ответным сообщением в чат{example_hint}.\n",
        "_Для отмены нажми кнопку ниже или отправь /cancel._",
    ]
    back_row = [InlineKeyboardButton("⬅️ Назад к вариантам", callback_data="artist:editaa")]
    if not from_report:
        back_row.append(InlineKeyboardButton("❌ Закрыть", callback_data="artist:close"))
    keyboard = [back_row]
    return "\n".join(lines), keyboard


def _render_track_artist_card(
    track_path: Path, track_idx: int, from_report: bool = False
) -> tuple[str, list[list[InlineKeyboardButton]], list[str]]:
    """Формирует карточку трека со списком артистов и кнопками управления."""
    try:
        audio = FLAC(str(track_path))
        track_num = audio.get("tracknumber", [""])[0].split("/")[0]
        title = audio.get("title", [track_path.stem])[0]
        artists = audio.get("artist", [])
    except Exception:
        track_num = ""
        title = track_path.stem
        artists = []

    display_title = f"{track_num}. {title}" if track_num else title
    lines = [
        f"🎵 *Трек:* `{display_title}`",
        f"📂 `{track_path.name}`\n",
        "👤 *Текущие артисты в теге ARTIST:*",
    ]

    if artists:
        for i, art in enumerate(artists, 1):
            lines.append(f"• {i}. `{art}`")
    else:
        lines.append("❌ _Артисты не указаны_")

    keyboard = []
    for i, art in enumerate(artists):
        keyboard.append([
            InlineKeyboardButton(f"❌ Удалить: {art}", callback_data=f"artist:del:{track_idx}:{i}")
        ])

    # Динамически находим других артистов релиза, которых ещё нет в тегах этого трека
    other_artists: list[str] = []
    try:
        info = get_album_artists_info(track_path.parent)
        for t in info.get("tracks", []):
            for a in t.get("artists", []):
                if a not in artists and a not in other_artists:
                    other_artists.append(a)
        if info.get("album_artist") and info["album_artist"] != "—":
            for a in info["album_artist"].split(","):
                a = a.strip()
                if a and a not in artists and a not in other_artists:
                    other_artists.append(a)
    except Exception:
        pass

    for i, art in enumerate(other_artists[:3]):
        keyboard.append([
            InlineKeyboardButton(f"➕ Добавить: {art}", callback_data=f"artist:qadd:{track_idx}:{i}")
        ])

    keyboard.append([
        InlineKeyboardButton("✍️ Задать артистов трека вручную", callback_data=f"artist:settr:{track_idx}"),
        InlineKeyboardButton("➕ Добавить имя", callback_data=f"artist:add:{track_idx}"),
    ])

    back_row = [InlineKeyboardButton("⬅️ К альбому", callback_data="artist:alb_back")]
    if not from_report:
        back_row.append(InlineKeyboardButton("❌ Закрыть", callback_data="artist:close"))
    keyboard.append(back_row)

    return "\n".join(lines), keyboard, other_artists


@_owner_only
async def cmd_artist(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /artist [запрос] — просмотр и редактирование артистов релиза в Library."""
    if not LIBRARY.exists():
        msg = await update.message.reply_text(f"❌ Папка {LIBRARY.name} не найдена.", parse_mode="Markdown")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    query_str = " ".join(ctx.args).strip() if ctx.args else ""

    all_albums = sorted([
        d for d in LIBRARY.iterdir()
        if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
    ])

    if not all_albums:
        msg = await update.message.reply_text(f"📭 В {LIBRARY.name} пока нет альбомов с FLAC-файлами.")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    if query_str:
        matches = [d for d in all_albums if query_str.lower() in d.name.lower()]
    else:
        matches = all_albums[-10:]

    if not matches:
        msg = await update.message.reply_text(f"❌ Ничего не найдено по запросу: `{query_str}`", parse_mode="Markdown")
        delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=4.0)
        delete_later(ctx.bot, OWNER_ID, update.message.message_id, delay=4.0)
        return

    if len(matches) == 1:
        try:
            await update.message.delete()
        except Exception:
            pass
        album_dir = matches[0]
        ctx.bot_data["artist_tracks"] = sorted(album_dir.rglob("*.flac"))
        ctx.bot_data["artist_album_path"] = album_dir
        ctx.bot_data.pop("artist_from_report", None)
        text, keyboard = _render_album_artists_view(album_dir, from_report=False)
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    lines = [f"👤 *Выбери альбом для настройки артистов ({len(matches)}):*\n"]
    buttons = []
    for i, d in enumerate(matches):
        artist, album = album_info(d)
        display = f"{artist} — {album}" if album else d.name
        lines.append(f"{i + 1}. `{d.name}` — {display}")
        buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"artist:alb:{i}"))

    lines.append("\n💬 _Напиши название релиза в чат для быстрого поиска._")
    ctx.bot_data["artist_albums"] = matches
    ctx.bot_data["artist_cmd_msg_id"] = update.message.message_id
    ctx.bot_data.pop("artist_from_report", None)
    keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="artist:close")])

    sent_msg = await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    ctx.bot_data[f"active_menu_{OWNER_ID}"] = {"cmd": "artist", "msg_id": sent_msg.message_id}


async def on_artist_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопок управления артистами."""
    query = update.callback_query
    if update.effective_user.id != OWNER_ID:
        await query.answer()
        return

    parts = query.data.split(":")
    action = parts[1]

    if action == "close":
        await query.answer()
        ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)
        if f"artist_action_{OWNER_ID}" in ctx.bot_data:
            del ctx.bot_data[f"artist_action_{OWNER_ID}"]
        try:
            await query.delete_message()
        except Exception:
            pass
        cmd_id = ctx.bot_data.pop("artist_cmd_msg_id", None)
        if cmd_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=cmd_id)
            except Exception:
                pass
        return

    if action in ("alb", "album"):
        ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)

    if action == "album":
        await query.answer()
        album_name = ":".join(parts[2:])
        album_dir = LIBRARY / album_name
        if not album_dir.exists():
            matches = [d for d in LIBRARY.iterdir() if d.is_dir() and d.name.lower() == album_name.lower()]
            if matches:
                album_dir = matches[0]

        if not album_dir.exists():
            await _safe_edit_message(query, f"❌ Альбом `{album_name}` не найден.")
            return

        tracks = sorted(album_dir.rglob("*.flac"))
        if not tracks:
            await _safe_edit_message(query, "❌ В альбоме нет FLAC-треков.")
            return

        ctx.bot_data["artist_tracks"] = tracks
        ctx.bot_data["artist_album_path"] = album_dir
        ctx.bot_data["artist_from_report"] = album_dir.name

        text, keyboard = _render_album_artists_view(album_dir, from_report=True)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "alb":
        await query.answer()
        idx = int(parts[2])
        albums = ctx.bot_data.get("artist_albums", [])
        if not (0 <= idx < len(albums)):
            return

        cmd_id = ctx.bot_data.pop("artist_cmd_msg_id", None)
        if cmd_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=cmd_id)
            except Exception:
                pass

        album_dir = albums[idx]
        tracks = sorted(album_dir.rglob("*.flac"))
        ctx.bot_data["artist_tracks"] = tracks
        ctx.bot_data["artist_album_path"] = album_dir
        ctx.bot_data.pop("artist_from_report", None)

        text, keyboard = _render_album_artists_view(album_dir, from_report=False)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "alb_back":
        await query.answer()
        if f"artist_action_{OWNER_ID}" in ctx.bot_data:
            del ctx.bot_data[f"artist_action_{OWNER_ID}"]
        album_dir = ctx.bot_data.get("artist_album_path")
        if not album_dir or not album_dir.exists():
            await _safe_edit_message(query, "⚠️ Сессия устарела. Используй /artist.")
            return

        ctx.bot_data["artist_tracks"] = sorted(album_dir.rglob("*.flac"))
        from_report = bool(ctx.bot_data.get("artist_from_report"))
        text, keyboard = _render_album_artists_view(album_dir, from_report=from_report)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "tr":
        await query.answer()
        idx = int(parts[2])
        tracks = ctx.bot_data.get("artist_tracks", [])
        if not (0 <= idx < len(tracks)):
            return

        track_path = tracks[idx]
        from_report = bool(ctx.bot_data.get("artist_from_report"))
        text, keyboard, other = _render_track_artist_card(track_path, idx, from_report=from_report)
        ctx.bot_data["artist_track_others"] = other
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "qadd":
        track_idx = int(parts[2])
        other_idx = int(parts[3])
        tracks = ctx.bot_data.get("artist_tracks", [])
        others = ctx.bot_data.get("artist_track_others", [])
        if not (0 <= track_idx < len(tracks)) or not (0 <= other_idx < len(others)):
            await query.answer()
            return

        track_path = tracks[track_idx]
        to_add = others[other_idx]
        add_track_artist(track_path, to_add)
        await query.answer(f"Добавлен: {to_add}")

        from_report = bool(ctx.bot_data.get("artist_from_report"))
        text, keyboard, other = _render_track_artist_card(track_path, track_idx, from_report=from_report)
        ctx.bot_data["artist_track_others"] = other
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "del":
        track_idx = int(parts[2])
        artist_idx = int(parts[3])
        tracks = ctx.bot_data.get("artist_tracks", [])
        if not (0 <= track_idx < len(tracks)):
            await query.answer()
            return

        track_path = tracks[track_idx]
        try:
            audio = FLAC(str(track_path))
            artists = audio.get("artist", [])
            if 0 <= artist_idx < len(artists):
                target_artist = artists[artist_idx]
                remove_track_artist(track_path, target_artist)
                await query.answer(f"Удалён: {target_artist}")
            else:
                await query.answer()
        except Exception as e:
            log.warning("Ошибка при удалении артиста: %s", e)
            await query.answer("Ошибка при удалении артиста", show_alert=True)
            return

        from_report = bool(ctx.bot_data.get("artist_from_report"))
        text, keyboard, other = _render_track_artist_card(track_path, track_idx, from_report=from_report)
        ctx.bot_data["artist_track_others"] = other
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "add":
        await query.answer()
        track_idx = int(parts[2])
        tracks = ctx.bot_data.get("artist_tracks", [])
        if not (0 <= track_idx < len(tracks)):
            return

        track_path = tracks[track_idx]
        album_dir = ctx.bot_data.get("artist_album_path")
        others = ctx.bot_data.get("artist_track_others", [])

        try:
            audio = FLAC(str(track_path))
            title = audio.get("title", [track_path.stem])[0]
            current = ", ".join(audio.get("artist", [])) or "—"
        except Exception:
            title = track_path.stem
            current = "—"

        other_hint = f" (например, `{others[0]}`)" if others else ""
        prompt = (
            f"➕ *Добавление артиста к треку:*\n🎵 *{title}*\n"
            f"👤 Текущие артисты: `{current}`\n\n"
            f"Отправь имя артиста (или нескольких через запятую) ответным сообщением{other_hint}.\n"
            f"Для отмены отправь команду /cancel."
        )
        prompt_msg = await ctx.bot.send_message(chat_id=OWNER_ID, text=prompt, parse_mode="Markdown")
        ctx.bot_data[f"artist_action_{OWNER_ID}"] = {
            "type": "add",
            "track_idx": track_idx,
            "track_path": str(track_path),
            "album_dir": str(album_dir) if album_dir else "",
            "card_msg_id": query.message.message_id,
            "prompt_msg_id": prompt_msg.message_id,
        }
        return

    if action == "editall":
        await query.answer()
        album_dir = ctx.bot_data.get("artist_album_path")
        if not album_dir or not album_dir.exists():
            await _safe_edit_message(query, "⚠️ Сессия устарела. Используй /artist.")
            return

        ctx.bot_data[f"artist_action_{OWNER_ID}"] = {
            "type": "editall",
            "album_dir": str(album_dir),
            "card_msg_id": query.message.message_id,
        }
        from_report = bool(ctx.bot_data.get("artist_from_report"))
        text, keyboard = _render_album_artists_all_prompt(album_dir, from_report=from_report)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "settr":
        await query.answer()
        track_idx = int(parts[2])
        tracks = ctx.bot_data.get("artist_tracks", [])
        if not (0 <= track_idx < len(tracks)):
            return

        track_path = tracks[track_idx]
        album_dir = ctx.bot_data.get("artist_album_path")

        try:
            audio = FLAC(str(track_path))
            title = audio.get("title", [track_path.stem])[0]
            current = ", ".join(audio.get("artist", [])) or "—"
        except Exception:
            title = track_path.stem
            current = "—"

        prompt = (
            f"✍️ *Ручной ввод артистов трека:*\n🎵 *{title}*\n"
            f"👤 Текущие артисты: `{current}`\n\n"
            f"Отправь верное имя исполнителя ответным сообщением.\n"
            f"Оно полностью заменит текущий список артистов этого трека.\n"
            f"Для отмены отправь команду /cancel."
        )
        prompt_msg = await ctx.bot.send_message(chat_id=OWNER_ID, text=prompt, parse_mode="Markdown")
        ctx.bot_data[f"artist_action_{OWNER_ID}"] = {
            "type": "settr",
            "track_idx": track_idx,
            "track_path": str(track_path),
            "album_dir": str(album_dir) if album_dir else "",
            "card_msg_id": query.message.message_id,
            "prompt_msg_id": prompt_msg.message_id,
        }
        return

    if action == "editaa":
        await query.answer()
        album_dir = ctx.bot_data.get("artist_album_path")
        if not album_dir or not album_dir.exists():
            await _safe_edit_message(query, "⚠️ Сессия устарела. Используй /artist.")
            return

        suggestions = get_album_artist_suggestions(album_dir)
        ctx.bot_data["artist_aa_suggestions"] = suggestions
        ctx.bot_data[f"artist_action_{OWNER_ID}"] = {
            "type": "editaa",
            "album_dir": str(album_dir),
            "card_msg_id": query.message.message_id,
        }

        from_report = bool(ctx.bot_data.get("artist_from_report"))
        text, keyboard = _render_album_artist_edit_card(album_dir, suggestions, from_report=from_report)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "customaa":
        await query.answer()
        album_dir = ctx.bot_data.get("artist_album_path")
        if not album_dir or not album_dir.exists():
            await _safe_edit_message(query, "⚠️ Сессия устарела. Используй /artist.")
            return

        ctx.bot_data[f"artist_action_{OWNER_ID}"] = {
            "type": "editaa",
            "album_dir": str(album_dir),
            "card_msg_id": query.message.message_id,
        }

        from_report = bool(ctx.bot_data.get("artist_from_report"))
        text, keyboard = _render_album_artist_custom_prompt(album_dir, from_report=from_report)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "setaa":
        val_or_clear = parts[2]
        album_dir = ctx.bot_data.get("artist_album_path")
        if not album_dir or not album_dir.exists():
            await query.answer("⚠️ Сессия устарела", show_alert=True)
            return

        if val_or_clear == "clear":
            new_aa = "-"
        else:
            suggestions = ctx.bot_data.get("artist_aa_suggestions", [])
            idx = int(val_or_clear)
            if not (0 <= idx < len(suggestions)):
                await query.answer()
                return
            new_aa = suggestions[idx]

        new_folder, status_text = update_album_artist(album_dir, new_aa)
        _update_album_references(ctx, album_dir, new_folder)
        if f"artist_action_{OWNER_ID}" in ctx.bot_data:
            del ctx.bot_data[f"artist_action_{OWNER_ID}"]

        toast = f"✅ Папка переименована в:\n{new_folder.name}" if new_folder != album_dir else f"✅ {status_text}"
        await query.answer(toast[:190], show_alert=(new_folder != album_dir))
        from_report = bool(ctx.bot_data.get("artist_from_report"))
        text, keyboard = _render_album_artists_view(new_folder, from_report=from_report)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return


@_owner_only
async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Сбрасывает ожидание ручного ввода текста или редактирования артистов."""
    try:
        await update.message.delete()
    except Exception:
        pass

    waiting_lyrics = f"lyrics_waiting_{OWNER_ID}"
    waiting_artist = f"artist_action_{OWNER_ID}"
    active_menu = f"active_menu_{OWNER_ID}"
    ctx.bot_data.pop(active_menu, None)

    cancelled = False
    if waiting_lyrics in ctx.bot_data:
        data = ctx.bot_data.pop(waiting_lyrics)
        prompt_id = data.get("prompt_msg_id") if isinstance(data, dict) else None
        if prompt_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=prompt_id)
            except Exception:
                pass
        cancelled = True

    if waiting_artist in ctx.bot_data:
        data = ctx.bot_data.pop(waiting_artist)
        prompt_id = data.get("prompt_msg_id") if isinstance(data, dict) else None
        if prompt_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=prompt_id)
            except Exception:
                pass
        cancelled = True

    text = "Действие отменено." if cancelled else "Нет активных действий для отмены."
    msg = await ctx.bot.send_message(chat_id=OWNER_ID, text=text)
    delete_later(ctx.bot, OWNER_ID, msg.message_id, delay=3.0)


async def on_lyrics_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопок управления текстами и нейросетевой синхронизацией."""
    query = update.callback_query
    if update.effective_user.id != OWNER_ID:
        await query.answer()
        return

    parts = query.data.split(":")
    action = parts[1]

    if action not in ("src", "albmiss", "libmiss"):
        await query.answer()

    if action == "close":
        ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)
        try:
            await query.delete_message()
        except Exception:
            pass
        cmd_id = ctx.bot_data.pop("lyrics_cmd_msg_id", None)
        if cmd_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=cmd_id)
            except Exception:
                pass
        return

    if action in ("alb", "album", "audit"):
        ctx.bot_data.pop(f"active_menu_{OWNER_ID}", None)

    if action == "audit":
        status_text = "⏳ Анализирую тексты по всей библиотеке..."
        await _safe_edit_message(query, status_text)
        loop = asyncio.get_running_loop()
        audit_data = await loop.run_in_executor(None, lambda: audit_library_lyrics(LIBRARY))
        ctx.bot_data["lyrics_audit_data"] = audit_data
        text, keyboard = _render_lyrics_audit_view(audit_data)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "alb_list":
        all_albums = sorted([
            d for d in LIBRARY.iterdir()
            if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
        ])
        matches = all_albums[-10:]
        ctx.bot_data["lyrics_albums"] = matches
        ctx.bot_data[f"active_menu_{OWNER_ID}"] = {"cmd": "lyrics", "msg_id": query.message.message_id}
        text, keyboard = _render_albums_menu(matches)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "album":
        album_name = parts[2]
        album_dir = LIBRARY / album_name
        if not album_dir.exists():
            matches = [d for d in LIBRARY.iterdir() if d.is_dir() and album_name.lower() in d.name.lower()]
            if matches:
                album_dir = matches[0]

        if not album_dir.exists():
            await _safe_edit_message(query, f"❌ Альбом `{album_name}` не найден.")
            return

        tracks = sorted(album_dir.rglob("*.flac"))
        if not tracks:
            await _safe_edit_message(query, "❌ В альбоме нет FLAC-треков.")
            return

        ctx.bot_data["lyrics_tracks"] = tracks
        ctx.bot_data["lyrics_album_path"] = album_dir
        ctx.bot_data["lyrics_from_report"] = album_dir.name

        text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks, from_report=True)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "alb":
        idx = int(parts[2])
        albums = ctx.bot_data.get("lyrics_albums", [])
        if not (0 <= idx < len(albums)):
            return

        cmd_id = ctx.bot_data.pop("lyrics_cmd_msg_id", None)
        if cmd_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=cmd_id)
            except Exception:
                pass

        album_dir = albums[idx]
        tracks = sorted(album_dir.rglob("*.flac"))
        ctx.bot_data["lyrics_tracks"] = tracks
        ctx.bot_data["lyrics_album_path"] = album_dir
        ctx.bot_data.pop("lyrics_from_report", None)

        text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks, from_report=False)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "back":
        album_dir = ctx.bot_data.get("lyrics_album_path")
        tracks = ctx.bot_data.get("lyrics_tracks")
        if not album_dir or not tracks:
            await _safe_edit_message(query, "⚠️ Сессия устарела. Используй /lyrics.")
            return

        from_report = bool(ctx.bot_data.get("lyrics_from_report"))
        text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks, from_report=from_report)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "tr":
        idx = int(parts[2])
        tracks = ctx.bot_data.get("lyrics_tracks", [])
        if not (0 <= idx < len(tracks)):
            return

        track_path = tracks[idx]
        from_report = bool(ctx.bot_data.get("lyrics_from_report"))
        text, keyboard = _render_track_lyrics_card(track_path, idx, from_report=from_report)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "src":
        source = parts[2]
        idx = int(parts[3])
        tracks = ctx.bot_data.get("lyrics_tracks", [])
        if not (0 <= idx < len(tracks)):
            await query.answer()
            return

        track_path = tracks[idx]
        try:
            audio = FLAC(str(track_path))
            arts = audio.get("artist", [])
            artist = ", ".join(arts) if arts else audio.get("albumartist", [""])[0]
            title = audio.get("title", [""])[0]
            album = audio.get("album", [""])[0]
            duration = int(audio.info.length) if hasattr(audio.info, "length") else 0
        except Exception:
            artist, title, album, duration = "", track_path.stem, "", 0

        source_names = {"lrclib": "LRCLIB", "lrcmux": "LRCMUX", "syncedlyrics": "SyncedLyrics", "genius": "Genius"}
        src_name = source_names.get(source, source)

        lm = LyricManager()
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(
            None, lambda: lm.find_lyrics(artist, title, album, duration, preferred_source=source)
        )

        if not res or not res.text:
            await query.answer(f"😕 На {src_name} текст не найден", show_alert=True)
            return

        # Проверяем, изменился ли текст
        lrc_path = track_path.with_suffix(".lrc")
        already_identical = False
        if lrc_path.exists():
            try:
                already_identical = lrc_path.read_text(encoding="utf-8").strip() == res.text.strip()
            except Exception:
                pass

        lrc_path.write_text(res.text, encoding="utf-8")
        try:
            lrc_path.chmod(0o664)
            os.utime(str(track_path), None)
        except Exception:
            pass

        if already_identical:
            await query.answer(f"ℹ️ Этот текст уже загружен из {src_name}")
        else:
            await query.answer(f"✅ Текст загружен из {src_name}!")

        from_report = bool(ctx.bot_data.get("lyrics_from_report"))
        text, keyboard = _render_track_lyrics_card(track_path, idx, from_report=from_report)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if action == "albmiss":
        album_dir = ctx.bot_data.get("lyrics_album_path")
        tracks = ctx.bot_data.get("lyrics_tracks", [])
        if not album_dir or not tracks:
            await query.answer("⚠️ Сессия устарела. Используй /lyrics.")
            return

        missing_tracks = [t for t in tracks if not t.with_suffix(".lrc").exists()]
        if not missing_tracks:
            await query.answer("ℹ️ В этом альбоме у всех треков уже есть тексты.", show_alert=True)
            return

        await query.answer("🌐 Начинаю поиск недостающих текстов...")
        ctx.bot_data["lyrics_batch_cancel"] = False
        loop = asyncio.get_running_loop()
        last_edit_time = 0.0

        def _on_albmiss_progress(cur: int, tot: int, tr_name: str):
            if ctx.bot_data.get("lyrics_batch_cancel"):
                return
            nonlocal last_edit_time
            now = time.time()
            if now - last_edit_time > 1.8 or cur == tot:
                last_edit_time = now
                pct = int((cur / tot) * 100)
                txt = (
                    f"⏳ *Поиск недостающих текстов: {cur}/{tot} ({pct}%)*\n\n"
                    f"🎵 Трек: `{tr_name}`\n"
                    f"_Поиск в LRCLIB, LRCMUX, SyncedLyrics, Genius..._"
                )
                kb = [[InlineKeyboardButton("⏹️ Остановить", callback_data="lyrics:stop_batch")]]
                try:
                    asyncio.run_coroutine_threadsafe(
                        _safe_edit_message(query, txt, reply_markup=InlineKeyboardMarkup(kb)),
                        loop,
                    )
                except Exception:
                    pass

        def _run_albmiss():
            lm = LyricManager()
            found = 0
            for i, tr in enumerate(missing_tracks):
                if ctx.bot_data.get("lyrics_batch_cancel"):
                    break
                _on_albmiss_progress(i + 1, len(missing_tracks), tr.name)
                try:
                    audio = FLAC(str(tr))
                    arts = audio.get("artist", [])
                    artist = ", ".join(arts) if arts else audio.get("albumartist", [""])[0]
                    title = audio.get("title", [""])[0]
                    album = audio.get("album", [""])[0]
                    dur = int(audio.info.length) if hasattr(audio.info, "length") else 0
                except Exception:
                    artist, title, album, dur = "", tr.stem, "", 0

                res = lm.find_lyrics(artist, title, album, dur)
                if res and res.text:
                    lrc_p = tr.with_suffix(".lrc")
                    lrc_p.write_text(res.text, encoding="utf-8")
                    try:
                        lrc_p.chmod(0o664)
                        os.utime(str(tr), None)
                    except Exception:
                        pass
                    found += 1
            return found

        found_count = await loop.run_in_executor(None, _run_albmiss)

        from_report = bool(ctx.bot_data.get("lyrics_from_report"))
        text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks, from_report=from_report)
        await _safe_edit_message(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

        conf = await ctx.bot.send_message(
            chat_id=OWNER_ID,
            text=f"🌐 Найдено текстов онлайн: {found_count} из {len(missing_tracks)}!",
        )
        delete_later(ctx.bot, OWNER_ID, conf.message_id, delay=4.0)
        return

    if action == "stop_batch":
        ctx.bot_data["lyrics_batch_cancel"] = True
        await _safe_edit_message(
            query,
            "⏹️ *Останавливаю процесс...*\n\n_Пожалуйста, подожди завершения текущего трека..._",
            reply_markup=None,
        )
        await _safe_answer_query(query, "Останавливаю процесс…")
        return

    if action == "libmiss":
        subaction = parts[2] if len(parts) > 2 else ""
        if subaction == "stop":
            ctx.bot_data["lyrics_libmiss_cancel"] = True
            await _safe_edit_message(
                query,
                "⏹️ *Останавливаю поиск текстов...*\n\n_Пожалуйста, подожди завершения текущего трека..._",
                reply_markup=None,
            )
            await _safe_answer_query(query, "Останавливаю поиск текстов…")
            return

        if ctx.bot_data.get("lyrics_libmiss_running"):
            await _safe_answer_query(query, "Поиск уже выполняется!", show_alert=True)
            return

        await _safe_answer_query(query)

        loop = asyncio.get_running_loop()
        audit_data = await loop.run_in_executor(None, lambda: audit_library_lyrics(LIBRARY))
        target_tracks = audit_data.get("missing", [])

        if not target_tracks:
            await _safe_answer_query(query, "ℹ️ Нет треков без текста.", show_alert=True)
            return

        ctx.bot_data["lyrics_libmiss_running"] = True
        ctx.bot_data["lyrics_libmiss_cancel"] = False
        last_edit_time = 0.0

        def _on_libmiss_progress(cur: int, tot: int, artist_title: str, alb: str, found: int, not_found: int):
            if ctx.bot_data.get("lyrics_libmiss_cancel"):
                return
            nonlocal last_edit_time
            now = time.time()
            if now - last_edit_time > 2.0 or cur == tot:
                last_edit_time = now
                pct = int((cur / tot) * 100)
                txt = (
                    f"⏳ *Поиск текстов по библиотеке: {cur}/{tot} ({pct}%)*\n\n"
                    f"🎵 Трек: *{artist_title}*\n"
                    f"💿 Альбом: `{alb}`\n"
                    f"✨ Найдено: *{found}* | ⚠️ Не найдено: *{not_found}*\n\n"
                    f"_Поиск в LRCLIB, LRCMUX, SyncedLyrics, Genius..._"
                )
                kb = [[InlineKeyboardButton("⏹️ Остановить", callback_data="lyrics:libmiss:stop")]]
                try:
                    asyncio.run_coroutine_threadsafe(
                        _safe_edit_message(query, txt, reply_markup=InlineKeyboardMarkup(kb)),
                        loop,
                    )
                except Exception:
                    pass

        def _run_libmiss():
            lm = LyricManager()
            found = 0
            not_found = 0
            try:
                for i, tr in enumerate(target_tracks):
                    if ctx.bot_data.get("lyrics_libmiss_cancel"):
                        break

                    try:
                        audio = FLAC(str(tr))
                        arts = audio.get("artist", [])
                        artist = ", ".join(arts) if arts else audio.get("albumartist", [""])[0]
                        title = audio.get("title", [""])[0]
                        alb = audio.get("album", [""])[0] or tr.parent.name
                        dur = int(audio.info.length) if hasattr(audio.info, "length") else 0
                    except Exception:
                        artist, title, alb, dur = "", tr.stem, tr.parent.name, 0
                    at_str = f"{artist} — {title}" if artist else title

                    _on_libmiss_progress(i + 1, len(target_tracks), at_str, alb, found, not_found)

                    res = lm.find_lyrics(artist, title, alb, dur)
                    if ctx.bot_data.get("lyrics_libmiss_cancel"):
                        break
                    if res and res.text:
                        lrc_p = tr.with_suffix(".lrc")
                        lrc_p.write_text(res.text, encoding="utf-8")
                        try:
                            lrc_p.chmod(0o664)
                            os.utime(str(tr), None)
                        except Exception:
                            pass
                        found += 1
                    else:
                        not_found += 1
            except Exception as e:
                log.error("Ошибка при поиске недостающих текстов: %s", e)
            return found, not_found

        try:
            found, not_found = await loop.run_in_executor(None, _run_libmiss)
        finally:
            ctx.bot_data["lyrics_libmiss_running"] = False

        was_cancelled = ctx.bot_data.pop("lyrics_libmiss_cancel", False)
        status_word = "остановлен" if was_cancelled else "завершён"

        if not was_cancelled:
            audit_data = await loop.run_in_executor(None, lambda: audit_library_lyrics(LIBRARY))
            ctx.bot_data["lyrics_audit_data"] = audit_data
        else:
            ctx.bot_data.pop("lyrics_audit_data", None)

        res_text = (
            f"✅ *Поиск текстов по библиотеке {status_word}!*\n\n"
            f"✨ Найдено и сохранено: *{found}*\n"
            f"⚠️ Не найдено в базах: *{not_found}*\n"
            f"📊 Всего в очереди было: *{len(target_tracks)}*"
        )
        kb = [
            [InlineKeyboardButton("📊 К аудиту библиотеки", callback_data="lyrics:audit")],
            [InlineKeyboardButton("📁 К альбомам", callback_data="lyrics:alb_list")],
            [InlineKeyboardButton("❌ Закрыть", callback_data="lyrics:close")],
        ]
        await _safe_edit_message(query, res_text, reply_markup=InlineKeyboardMarkup(kb))
        return

    if action == "man":
        idx = int(parts[2])
        tracks = ctx.bot_data.get("lyrics_tracks", [])
        if not (0 <= idx < len(tracks)):
            return

        track_path = tracks[idx]

        try:
            audio = FLAC(str(track_path))
            arts = audio.get("artist", [])
            artist = ", ".join(arts) if arts else audio.get("albumartist", [""])[0]
            title = audio.get("title", [""])[0]
        except Exception:
            artist, title = "", track_path.stem

        name_str = f"{artist} — {title}" if artist else title
        prompt = (
            f"✍️ *Ручной ввод текста для трека:*\n🎵 *{name_str}*\n\n"
            f"Отправь мне текст песни сообщением в ответ или прикрепи файл `.lrc` документом.\n"
            f"Для отмены отправь команду /cancel."
        )
        prompt_msg = await ctx.bot.send_message(chat_id=OWNER_ID, text=prompt, parse_mode="Markdown")
        ctx.bot_data[f"lyrics_waiting_{OWNER_ID}"] = {
            "track_path": str(track_path),
            "track_idx": idx,
            "prompt_msg_id": prompt_msg.message_id,
            "card_msg_id": query.message.message_id,
        }
        return


async def on_owner_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает присланные владельцем файлы/текст для lyrics или artist."""
    if update.effective_user.id != OWNER_ID:
        return

    # Всегда удаляем входящее сообщение пользователя с текстом/файлом, чтобы не засорять историю чата
    try:
        await update.message.delete()
    except Exception:
        pass

    # 1. Режим настройки артистов
    artist_key = f"artist_action_{OWNER_ID}"
    if artist_key in ctx.bot_data:
        action_data = ctx.bot_data.pop(artist_key)

        prompt_id = action_data.get("prompt_msg_id")
        if prompt_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=prompt_id)
            except Exception:
                pass

        user_text = (update.message.text or "").strip()
        if not user_text:
            warn = await ctx.bot.send_message(chat_id=OWNER_ID, text="❌ Пустой ввод. Действие отменено.")
            delete_later(ctx.bot, OWNER_ID, warn.message_id, delay=3.0)
            return

        if action_data["type"] == "add":
            track_path = Path(action_data["track_path"])
            updated_artists = add_track_artist(track_path, user_text)
            card_id = action_data.get("card_msg_id")
            track_idx = action_data.get("track_idx")

            if card_id and track_idx is not None:
                from_report = bool(ctx.bot_data.get("artist_from_report"))
                text, keyboard, other = _render_track_artist_card(track_path, track_idx, from_report=from_report)
                ctx.bot_data["artist_track_others"] = other
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=card_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    pass

            conf = await ctx.bot.send_message(
                chat_id=OWNER_ID,
                text=f"✅ Артист `{user_text}` добавлен в трек!",
                parse_mode="Markdown",
            )
            delete_later(ctx.bot, OWNER_ID, conf.message_id, delay=3.0)
            return

        if action_data["type"] == "settr":
            track_path = Path(action_data["track_path"])
            updated_artists = set_track_artists(track_path, user_text)
            card_id = action_data.get("card_msg_id")
            track_idx = action_data.get("track_idx")

            if card_id and track_idx is not None:
                from_report = bool(ctx.bot_data.get("artist_from_report"))
                text, keyboard, other = _render_track_artist_card(track_path, track_idx, from_report=from_report)
                ctx.bot_data["artist_track_others"] = other
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=card_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    pass

            conf = await ctx.bot.send_message(
                chat_id=OWNER_ID,
                text=f"✅ Артисты трека заменены на: `{user_text}`",
                parse_mode="Markdown",
            )
            delete_later(ctx.bot, OWNER_ID, conf.message_id, delay=3.0)
            return

        if action_data["type"] == "editall":
            album_dir = Path(action_data["album_dir"])
            new_folder, status_text = replace_all_tracks_artist(album_dir, user_text)
            _update_album_references(ctx, album_dir, new_folder)

            card_id = action_data.get("card_msg_id")
            if card_id:
                from_report = bool(ctx.bot_data.get("artist_from_report"))
                text, keyboard = _render_album_artists_view(new_folder, from_report=from_report)
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=card_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    pass

            conf = await ctx.bot.send_message(
                chat_id=OWNER_ID,
                text=f"✅ *{status_text}*\n📂 `{new_folder.name}`",
                parse_mode="Markdown",
            )
            delete_later(ctx.bot, OWNER_ID, conf.message_id, delay=3.0)
            return

        if action_data["type"] == "editaa":
            album_dir = Path(action_data["album_dir"])
            new_folder, status_text = update_album_artist(album_dir, user_text)
            _update_album_references(ctx, album_dir, new_folder)

            card_id = action_data.get("card_msg_id")
            if card_id:
                from_report = bool(ctx.bot_data.get("artist_from_report"))
                text, keyboard = _render_album_artists_view(new_folder, from_report=from_report)
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=card_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    pass

            conf = await ctx.bot.send_message(
                chat_id=OWNER_ID,
                text=f"✅ *{status_text}*\n📂 `{new_folder.name}`",
                parse_mode="Markdown",
            )
            delete_later(ctx.bot, OWNER_ID, conf.message_id, delay=3.0)
            return

    # 2. Режим ручного ввода текста песни (.lrc или текст)
    waiting_key = f"lyrics_waiting_{OWNER_ID}"
    if waiting_key in ctx.bot_data:
        waiting_data = ctx.bot_data.pop(waiting_key)

        prompt_id = waiting_data.get("prompt_msg_id")
        if prompt_id:
            try:
                await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=prompt_id)
            except Exception:
                pass

        track_path = Path(waiting_data["track_path"])
        track_idx = waiting_data["track_idx"]
        card_id = waiting_data.get("card_msg_id")

        text_to_save = ""
        if update.message.document:
            doc = update.message.document
            file_obj = await doc.get_file()
            file_bytes = await file_obj.download_as_bytearray()
            text_to_save = file_bytes.decode("utf-8", errors="replace").strip()
        elif update.message.text:
            text_to_save = update.message.text.strip()

        if not text_to_save:
            warn = await ctx.bot.send_message(chat_id=OWNER_ID, text="❌ Пустой текст. Отправка отменена.")
            delete_later(ctx.bot, OWNER_ID, warn.message_id, delay=3.0)
            return

        lrc_path = track_path.with_suffix(".lrc")
        lrc_path.write_text(text_to_save, encoding="utf-8")
        try:
            lrc_path.chmod(0o664)
            os.utime(str(track_path), None)
        except Exception:
            pass

        if card_id:
            from_report = bool(ctx.bot_data.get("lyrics_from_report"))
            text, keyboard = _render_track_lyrics_card(track_path, track_idx, from_report=from_report)
            try:
                await ctx.bot.edit_message_text(
                    chat_id=OWNER_ID,
                    message_id=card_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception:
                pass

        is_sync = has_timestamps(text_to_save)
        status = "синхронизированный 🎤" if is_sync else "обычный 📄"
        conf = await ctx.bot.send_message(
            chat_id=OWNER_ID,
            text=f"✅ Текст сохранён ({status}) для `{track_path.name}`!",
            parse_mode="Markdown",
        )
        delete_later(ctx.bot, OWNER_ID, conf.message_id, delay=3.0)
        return

    # 3. Интерактивный поиск по тексту для активного меню (/cover, /lyrics, /artist, /upgrade, /scan)
    active_menu_key = f"active_menu_{OWNER_ID}"
    if active_menu_key in ctx.bot_data and update.message.text:
        menu_info = ctx.bot_data[active_menu_key]
        query_str = update.message.text.strip()
        cmd_type = menu_info.get("cmd")
        msg_id = menu_info.get("msg_id")

        if cmd_type == "cover":
            all_albums = sorted([
                d for d in LIBRARY.iterdir()
                if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
            ])
            matches = [d for d in all_albums if query_str.lower() in d.name.lower()]
            if len(matches) == 1:
                ctx.bot_data.pop(active_menu_key, None)
                try:
                    await ctx.bot.delete_message(chat_id=OWNER_ID, message_id=msg_id)
                except Exception:
                    pass
                await _on_new_release(matches[0], ctx.application, is_recover=True)
                return
            elif len(matches) > 1:
                ctx.bot_data["recover_folders"] = matches
                lines = [f"🖼️ *Выбери альбом для смены обложки по запросу `{query_str}` ({len(matches)}):*\n"]
                buttons = []
                for i, d in enumerate(matches):
                    artist, album = album_info(d)
                    display = f"{artist} — {album}" if album else d.name
                    lines.append(f"{i + 1}. `{d.name}` — {display}")
                    buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"recover:{i}"))
                lines.append("\n💬 _Напиши другое название в чат для изменения поиска._")
                keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
                keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="recover:close")])
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text="\n".join(lines),
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    pass
                return
            else:
                try:
                    kb = [[InlineKeyboardButton("❌ Закрыть", callback_data="recover:close")]]
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text=f"❌ Ничего не найдено по запросу: `{query_str}`\n\n💬 Напиши другое название для поиска.",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(kb),
                    )
                except Exception:
                    pass
                return

        elif cmd_type == "lyrics":
            all_albums = sorted([
                d for d in LIBRARY.iterdir()
                if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
            ])
            matches = [d for d in all_albums if query_str.lower() in d.name.lower()]
            if len(matches) == 1:
                ctx.bot_data.pop(active_menu_key, None)
                album_dir = matches[0]
                tracks = sorted(album_dir.rglob("*.flac"))
                ctx.bot_data["lyrics_tracks"] = tracks
                ctx.bot_data["lyrics_album_path"] = album_dir
                ctx.bot_data.pop("lyrics_from_report", None)
                text, keyboard = _render_album_lyrics_tracklist(album_dir, tracks, from_report=False)
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    pass
                return
            elif len(matches) > 1:
                ctx.bot_data["lyrics_albums"] = matches
                text, keyboard = _render_albums_menu(matches)
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    pass
                return
            else:
                try:
                    kb = [
                        [InlineKeyboardButton("📊 Аудит текстов библиотеки", callback_data="lyrics:audit")],
                        [InlineKeyboardButton("❌ Закрыть", callback_data="lyrics:close")],
                    ]
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text=f"❌ Ничего не найдено по запросу: `{query_str}`\n\n💬 Напиши другое название для поиска.",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(kb),
                    )
                except Exception:
                    pass
                return

        elif cmd_type == "artist":
            all_albums = sorted([
                d for d in LIBRARY.iterdir()
                if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
            ])
            matches = [d for d in all_albums if query_str.lower() in d.name.lower()]
            if len(matches) == 1:
                ctx.bot_data.pop(active_menu_key, None)
                album_dir = matches[0]
                ctx.bot_data["artist_tracks"] = sorted(album_dir.rglob("*.flac"))
                ctx.bot_data["artist_album_path"] = album_dir
                ctx.bot_data.pop("artist_from_report", None)
                text, keyboard = _render_album_artists_view(album_dir, from_report=False)
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    pass
                return
            elif len(matches) > 1:
                ctx.bot_data["artist_albums"] = matches
                lines = [f"👤 *Выбери альбом для настройки артистов по запросу `{query_str}` ({len(matches)}):*\n"]
                buttons = []
                for i, d in enumerate(matches):
                    artist, album = album_info(d)
                    display = f"{artist} — {album}" if album else d.name
                    lines.append(f"{i + 1}. `{d.name}` — {display}")
                    buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"artist:alb:{i}"))
                lines.append("\n💬 _Напиши другое название в чат для изменения поиска._")
                keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
                keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="artist:close")])
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text="\n".join(lines),
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception:
                    pass
                return
            else:
                try:
                    kb = [[InlineKeyboardButton("❌ Закрыть", callback_data="artist:close")]]
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text=f"❌ Ничего не найдено по запросу: `{query_str}`\n\n💬 Напиши другое название для поиска.",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(kb),
                    )
                except Exception:
                    pass
                return

        elif cmd_type == "upgrade":
            albums = sorted([
                d for d in LIBRARY.iterdir()
                if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.rglob("*.flac") if f.is_file())
            ])
            matches = [d for d in albums if query_str.lower() in d.name.lower()]
            if not matches:
                try:
                    kb = [[InlineKeyboardButton("❌ Отмена", callback_data="upgrade:close")]]
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text=f"❌ В `{LIBRARY.name}` не найдено альбомов по запросу: `{query_str}`\n\n💬 Напиши другое название для фильтрации.",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(kb),
                    )
                except Exception:
                    pass
                return

            ctx.bot_data["upgrade_target_albums"] = [str(a) for a in matches]
            target_desc = f"По запросу: `{query_str}` ({len(matches)} альбомов)"
            text = (
                f"📁 Каталог: `{LIBRARY.name}`\n"
                f"💿 Режим: *{target_desc}*\n\n"
                "*Выбери режим идеализации:*\n\n"
                "• ▶️ *Обычный (мягкий)* — быстро заполнит только пропуски: найдет недостающие обложки, недостающие тексты и проставит ReplayGain. Существующие файлы не перезаписывает.\n\n"
                "• ⚡ *Force (полная пересборка)* — с нуля очистит теги от мусора (извлечёт фиты из названий), перекачает Hi-Res обложки, обновит тексты (с защитой караоке от даунгрейда) и заново рассчитает ReplayGain.\n\n"
                "💬 _Напиши другое название в чат для изменения фильтра._"
            )
            keyboard = [
                [InlineKeyboardButton("▶️ Обычный апгрейд", callback_data="upgrade:start:normal")],
                [InlineKeyboardButton("⚡ Полная пересборка (Force)", callback_data="upgrade:start:force")],
                [InlineKeyboardButton("❌ Отмена", callback_data="upgrade:close")],
            ]
            try:
                await ctx.bot.edit_message_text(
                    chat_id=OWNER_ID,
                    message_id=msg_id,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception:
                pass
            return

        elif cmd_type == "scan":
            folders = sorted([
                d for d in INBOX.iterdir()
                if d.is_dir() and any(f.suffix.lower() == ".flac" for f in d.iterdir() if f.is_file())
            ])
            matches = [d for d in folders if query_str.lower() in d.name.lower()]
            if not matches:
                try:
                    kb = [[InlineKeyboardButton("❌ Закрыть", callback_data="scan:close")]]
                    await ctx.bot.edit_message_text(
                        chat_id=OWNER_ID,
                        message_id=msg_id,
                        text=f"❌ В `{INBOX.name}` не найдено папок по запросу: `{query_str}`\n\n💬 Напиши другое название для поиска.",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(kb),
                    )
                except Exception:
                    pass
                return

            lines = [f"📥 *Папки в {INBOX.name} по запросу `{query_str}` ({len(matches)}):*\n"]
            buttons = []
            for i, d in enumerate(matches):
                artist, album = album_info(d)
                n = count_flacs(d)
                display = f"{artist} — {album}" if album else d.name
                lines.append(f"{i + 1}. `{d.name}` — {display} ({n} треков)")
                buttons.append(InlineKeyboardButton(str(i + 1), callback_data=f"scan:{i}"))

            lines.append("\n💬 _Напиши другое имя папки в чат для изменения поиска._")
            ctx.bot_data["scan_folders"] = matches
            keyboard = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
            keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="scan:close")])
            try:
                await ctx.bot.edit_message_text(
                    chat_id=OWNER_ID,
                    message_id=msg_id,
                    text="\n".join(lines),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            except Exception:
                pass
            return


# ── Запуск ────────────────────────────────────────────────────────────────────

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик ошибок Telegram."""
    err = context.error
    if isinstance(err, BadRequest) and "Query is too old" in str(err):
        log.debug("Игнорируем устаревший callback query: %s", err)
        return
    if isinstance(err, (NetworkError, TimedOut)):
        log.warning("Временный сетевой сбой Telegram API (%s): %s", type(err).__name__, err)
        return
    log.error("Необработанная ошибка Telegram: %s", err, exc_info=err)


async def post_init(app: Application) -> None:
    """Запускает Inbox Watcher после инициализации бота."""
    async def on_release(folder: Path) -> None:
        await _on_new_release(folder, app)

    def is_busy() -> bool:
        session = _sessions.get(OWNER_ID)
        return session is not None and session.state != SessionState.DONE

    asyncio.create_task(watch_inbox(INBOX, on_release, is_busy=is_busy))
    log.info("Inbox watcher задача создана")

    try:
        msg = await app.bot.send_message(
            chat_id=OWNER_ID,
            text=f"🟢 NaviSan бот запущен. Слежу за {INBOX.name}.",
        )
        delete_later(app.bot, OWNER_ID, msg.message_id, delay=5.0)
    except Exception:
        log.warning("Не удалось отправить приветственное сообщение")


def main() -> None:
    request = HTTPXRequest(connect_timeout=20.0, read_timeout=20.0)
    app = Application.builder().token(TOKEN).request(request).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("cover", cmd_cover))
    app.add_handler(CommandHandler("lyrics", cmd_lyrics))
    app.add_handler(CommandHandler("artist", cmd_artist))
    app.add_handler(CommandHandler("process", cmd_process))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(on_upgrade_callback, pattern=r"^upgrade:"))
    app.add_handler(CallbackQueryHandler(on_cover_callback, pattern=r"^cover:"))
    app.add_handler(CallbackQueryHandler(on_scan_callback, pattern=r"^scan:"))
    app.add_handler(CallbackQueryHandler(on_recover_callback, pattern=r"^recover:"))
    app.add_handler(CallbackQueryHandler(on_lyrics_callback, pattern=r"^lyrics:"))
    app.add_handler(CallbackQueryHandler(on_artist_callback, pattern=r"^artist:"))
    app.add_handler(CallbackQueryHandler(on_report_callback, pattern=r"^report:"))

    # Сообщения владельца (текст или документы .lrc для ручного ввода текста песни или имени артиста)
    app.add_handler(MessageHandler((filters.TEXT | filters.Document.ALL) & ~filters.COMMAND, on_owner_message))

    app.add_error_handler(global_error_handler)

    app.post_init = post_init

    log.info("NaviSan бот запускается (owner=%d, inbox=%s, library=%s)",
             OWNER_ID, INBOX, LIBRARY)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
