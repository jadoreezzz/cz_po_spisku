"""Telegram-бот: присылаете PDF-этикетки ЧЗ (файлами или zip) + список артикулов —
получаете один PDF для печати, страницы в порядке списка.

Бот НЕ создаёт коды ЧЗ: он только переупорядочивает страницы уже существующих
этикеток. Исходные файлы не изменяются.

Запуск:  ./run_bot.sh   (токен в ~/.cz_bot_token, доступ в ~/.cz_bot_allowed_users)
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import Conflict, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import pool_store
from folders import get_roots, resolve_folder
from order_file import make_template, parse_order_file
from message_parser import parse_message
from pdf_builder import BuildReport, FolderSource, build_from_source, make_output_name
from pool import (ARCHIVE_SUFFIXES, ArchiveError, Pool, available_tools,
                  collect_from_archive, load_pool)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("cz-bot")

TOKEN_FILE = Path.home() / ".cz_bot_token"
ALLOWED_FILE = Path.home() / ".cz_bot_allowed_users"
FINALIZE_DELAY = 4.0  # секунд тишины после последнего файла — значит, загрузка закончилась

HELP = (
    "Как пользоваться:\n\n"
    "1. Перетащите боту свои PDF-этикетки ЧЗ — хоть 60 штук сразу, "
    "или архивом — .zip, .rar, .7z.\n"
    "2. Пришлите список артикулов — текстом (можно с маркерами `ozon` / `yandex`, "
    "если списка два) или файлом Excel/CSV, который заполняет склад. "
    "Текстовый список можно написать прямо в подписи к архиву — "
    "тогда всё делается одним сообщением.\n"
    "3. Получаете единый PDF для печати: страницы в порядке списка, "
    "повтор в списке = ещё одна страница.\n\n"
    "Пример списка:\n"
    "RUSH114B-XXL\nRUSH083B-XXL\n\nozon\n\nRUSH079B-L\nV-SHORT001M-L\n\nyandex\n\n"
    "Загруженные этикетки бот склеивает в один «пул» и присылает его вам — "
    "этот файл можно сохранить и в следующий раз прислать вместо всех этикеток: "
    "индекс артикулов зашит внутрь него.\n\n"
    "Команды:\n"
    "/pools — сохранённые пулы, /pool <имя> — переключиться\n"
    "/go — собрать пул из уже присланных файлов, не дожидаясь\n"
    "/template — шаблон Excel для списка заказа\n"
    "/reset — забыть загруженное\n"
    "/whoami — ваш Telegram ID"
)


# ---------------------------------------------------------------- доступ

def allowed_users() -> set[int]:
    raw = os.environ.get("CZ_BOT_ALLOWED_USERS", "")
    if not raw and ALLOWED_FILE.exists():
        raw = ALLOWED_FILE.read_text(encoding="utf-8")
    return {int(p.strip()) for p in raw.replace("\n", ",").split(",") if p.strip().isdigit()}


def is_allowed(update: Update) -> bool:
    ids = allowed_users()
    user = update.effective_user
    return bool(ids) and user is not None and user.id in ids


async def deny(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    if allowed_users():
        await update.message.reply_text(f"Доступ запрещён. Ваш ID: {uid}")
    else:
        await update.message.reply_text(
            "Бот не настроен: не задан список разрешённых пользователей.\n"
            f"Ваш Telegram ID: {uid}\n"
            f"Добавьте его в {ALLOWED_FILE} (или в CZ_BOT_ALLOWED_USERS) и перезапустите бота."
        )


# ---------------------------------------------------------------- сборка

@dataclass
class Job:
    marketplace: str
    codes: list[str]


def _counted(codes: list[str]) -> list[tuple[str, int]]:
    """Коды с числом вхождений, в порядке первого появления."""
    counts: dict[str, int] = {}
    for c in codes:
        counts[c] = counts.get(c, 0) + 1
    return list(counts.items())


def _qty(n: int) -> str:
    return f"нужно {n} шт, " if n > 1 else ""


def format_summary(results: list[tuple["Job", BuildReport]], source_name: str) -> str:
    """Короткий итог: «ozon 29/31, yandex 8/8.» + причины, если что-то не сошлось."""
    head = ", ".join(
        f"{job.marketplace.lower()} {rep.found}/{rep.total}" for job, rep in results
    ) + "."
    lines = [head]

    missing: list[str] = []
    unreadable: list[str] = []
    short: list[str] = []
    fallback: list[str] = []
    for _job, rep in results:
        missing += rep.missing_codes
        unreadable += rep.unreadable_codes
        for code, (need, have) in rep.short_pages.items():
            short.append(f"{code} (нужно {need}, в файле {have})")
        pretty = {
            "fallback_size_field": "размер отдельным полем",
            "fallback_cyrillic_m": "кириллическая «М» в размере",
        }
        fallback += [f"{c} ({pretty.get(m, m)})" for c, m in rep.fallback_used.items()]

    if missing:
        lines.append(
            f"не найдено в папке {source_name}: "
            + ", ".join(f"{c} ({_qty(n)}файла нет вообще)" for c, n in _counted(missing))
            + "."
        )
    if unreadable:
        lines.append(
            f"не читается в папке {source_name}: "
            + ", ".join(f"{c} ({_qty(n)}файл повреждён или занят)" for c, n in _counted(unreadable))
            + "."
        )
    if short:
        lines.append(
            "не хватило страниц: " + ", ".join(dict.fromkeys(short))
            + " — последняя страница продублирована, проверьте перед печатью."
        )
    if fallback:
        lines.append("найдено по фолбэку: " + ", ".join(dict.fromkeys(fallback)) + ".")
    return "\n".join(lines)


def _run_jobs(jobs: list[Job], source, source_name: str, out_dir: Path):
    results = []
    for job in jobs:
        target = out_dir / make_output_name(job.marketplace, source_name)
        results.append((job, build_from_source(job.codes, source, target)))
    return results


async def build_and_send(update: Update, jobs: list[Job], source, source_name: str,
                         notes: list[str]) -> None:
    msg = update.message
    chat_id = msg.chat_id
    await msg.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
    try:
        results = await asyncio.to_thread(
            _run_jobs, jobs, source, source_name, pool_store.out_dir(chat_id)
        )
    except Exception as e:  # noqa: BLE001
        log.exception("build failed")
        await msg.reply_text(f"Ошибка при сборке: {e}")
        return

    for _job, report in results:
        path = Path(report.output_path)  # type: ignore[arg-type]
        await msg.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        with open(path, "rb") as fh:
            await msg.reply_document(
                document=fh,
                filename=path.name,
                caption=f"НА ПЕЧАТЬ — {_job.marketplace}: {report.found} стр. в порядке списка.",
            )

    text = list(notes) + [format_summary(results, source_name)]
    await msg.reply_text("\n\n".join(text))


# ---------------------------------------------------------------- пул


def jobs_from(parsed) -> list["Job"]:
    jobs = []
    if parsed.ozon:
        jobs.append(Job("OZON", parsed.ozon))
    if parsed.yandex:
        jobs.append(Job("YANDEX", parsed.yandex))
    return jobs


def pool_name_from(caption: str, fallback: str) -> str:
    """Подпись к файлу — это имя пула, только если это не список артикулов."""
    parsed = parse_message(caption)
    if not parsed.is_empty:          # в подписи прислали список — имя берём из имени файла
        return fallback
    first = next((l.strip() for l in caption.splitlines() if l.strip()), "")
    return first[:40] or fallback


async def send_pool_file(update: Update, pool: Pool) -> None:
    with open(pool.path, "rb") as fh:
        await update.message.reply_document(
            document=fh,
            filename=pool.path.name,
            caption="Единый PDF со всеми этикетками. Сохраните его — "
                    "в следующий раз можно прислать только его вместо всех файлов.",
        )


async def finalize_inbox(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                         name: str | None = None, announce: bool = True,
                         send_file: bool = True) -> Pool | None:
    """Склеить накопленные файлы в пул."""
    chat_id = update.effective_chat.id
    sources = pool_store.inbox_files(chat_id)
    if not sources:
        return None
    name = name or ctx.chat_data.pop("pool_name", None) or datetime.now().strftime("пул %d.%m %H:%M")
    pool, unreadable = await asyncio.to_thread(pool_store.save_pool, chat_id, sources, name)
    pool_store.clear_inbox(chat_id)
    ctx.chat_data["pool_name"] = None

    if announce:
        text = (
            f"Пул «{pool.name}»: файлов {len(pool.filenames)}, страниц {pool.page_count}."
        )
        if unreadable:
            text += "\nНе удалось прочитать: " + ", ".join(unreadable)
        await update.message.reply_text(text)
        if send_file:
            await send_pool_file(update, pool)
    return pool


async def _finalize_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Отложенная сборка пула: сработает, когда файлы перестанут приходить."""
    data = ctx.job.data
    update: Update = data["update"]
    pending = ctx.chat_data.get("pending_jobs")
    pool = await finalize_inbox(update, ctx, send_file=not pending)
    if pool is None:
        return
    pending = ctx.chat_data.pop("pending_jobs", None)
    if pending:
        await build_and_send(update, pending, pool, pool.name, ["Список уже был — собираю по нему."])


def schedule_finalize(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    name = f"finalize-{update.effective_chat.id}"
    for job in ctx.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    ctx.job_queue.run_once(_finalize_job, FINALIZE_DELAY, name=name, data={"update": update},
                           chat_id=update.effective_chat.id)


async def active_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE, folder_query: str | None):
    """Откуда брать этикетки: накопленные файлы -> активный пул -> (опционально) папка на диске."""
    chat_id = update.effective_chat.id
    if pool_store.inbox_files(chat_id):
        for job in ctx.job_queue.get_jobs_by_name(f"finalize-{chat_id}"):
            job.schedule_removal()
        pool = await finalize_inbox(update, ctx)
        if pool:
            return pool, pool.name, []

    pool = pool_store.get_active(chat_id)
    if pool:
        return pool, pool.name, []

    if folder_query:  # запасной режим: папка на этом же компьютере
        resolved = resolve_folder(folder_query)
        if resolved.status == "ok":
            return FolderSource(resolved.match), resolved.match.name, [
                f"Беру этикетки из папки {resolved.match.name} на компьютере."
            ]
        if resolved.status == "ambiguous":
            names = "\n".join(f"• {c.name} ({c.pdf_count} PDF)" for c in resolved.candidates)
            await update.message.reply_text(f"Подходит несколько папок, уточните:\n{names}")
            return None, "", []
        await update.message.reply_text(
            f"Папка «{folder_query}» не найдена в {', '.join(str(r) for r in get_roots())}."
        )
        return None, "", []

    return None, "", []


async def download_doc(update: Update, doc, target: Path) -> bool:
    """Скачать вложение и убедиться, что оно скачалось целиком.

    Обрезанная загрузка выглядит как «битый архив», поэтому сверяем размер
    с тем, что обещал Telegram, и пробуем ещё раз.
    """
    got = -1
    for attempt in (1, 2):
        tg_file = await fetch_file(update, doc)
        if tg_file is None:
            return False
        await tg_file.download_to_drive(str(target))
        got = target.stat().st_size if target.exists() else 0
        if not doc.file_size or got == doc.file_size:
            return True
        log.warning("докачка %s: ожидалось %s байт, получено %s (попытка %s)",
                    doc.file_name, doc.file_size, got, attempt)
    await update.message.reply_text(
        f"Файл «{doc.file_name}» скачался не полностью: {got} из {doc.file_size} байт. "
        "Пришлите его ещё раз."
    )
    return False


async def fetch_file(update: Update, doc):
    """Скачать вложение. У Bot API лимит 20 МБ на скачивание — объясняем, если не влезло."""
    try:
        return await doc.get_file()
    except TelegramError as e:
        await update.message.reply_text(
            f"Не смог скачать «{doc.file_name}»: {e}\n"
            "Telegram отдаёт ботам файлы не больше 20 МБ — пришлите этикетки "
            "отдельными файлами или разбейте архив на части."
        )
        return None


# ---------------------------------------------------------------- хендлеры

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    await update.message.reply_text(HELP)


async def cmd_whoami(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    await update.message.reply_text(f"Ваш Telegram ID: {u.id}" if u else "?")


async def cmd_pools(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    chat_id = update.effective_chat.id
    pools = pool_store.list_pools(chat_id)
    if not pools:
        await update.message.reply_text("Пулов пока нет. Пришлите PDF-этикетки или .zip.")
        return
    active = pool_store.get_active(chat_id)
    active_path = active.path if active else None
    lines = [
        ("→ " if p == active_path else "• ") + f"{p.stem} ({datetime.fromtimestamp(p.stat().st_mtime):%d.%m %H:%M})"
        for p in pools
    ]
    await update.message.reply_text("Пулы этикеток:\n" + "\n".join(lines) +
                                    "\n\nПереключиться: /pool <имя>")


async def cmd_pool(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    chat_id = update.effective_chat.id
    arg = " ".join(ctx.args or []).strip()
    if not arg:
        pool = pool_store.get_active(chat_id)
        await update.message.reply_text(
            f"Активный пул: «{pool.name}» — {len(pool.filenames)} файлов, {pool.page_count} страниц."
            if pool else "Активного пула нет. Пришлите PDF-этикетки или .zip."
        )
        return
    path = pool_store.find_pool(chat_id, arg)
    if not path:
        await update.message.reply_text(f"Пул «{arg}» не найден. Список: /pools")
        return
    pool_store.set_active(chat_id, path)
    pool = pool_store.get_active(chat_id)
    await update.message.reply_text(
        f"Активный пул: «{pool.name}» — {len(pool.filenames)} файлов, {pool.page_count} страниц."
        if pool else f"Не удалось прочитать пул «{arg}»."
    )


async def cmd_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    for job in ctx.job_queue.get_jobs_by_name(f"finalize-{update.effective_chat.id}"):
        job.schedule_removal()
    pool = await finalize_inbox(update, ctx, name=" ".join(ctx.args or []).strip() or None)
    if pool is None:
        active = pool_store.get_active(update.effective_chat.id)
        await update.message.reply_text(
            f"Новых файлов нет. Активный пул «{active.name}»: {len(active.filenames)} файлов, "
            f"{active.page_count} страниц — присылайте список, соберу PDF на печать."
            if active else "Новых файлов нет. Пришлите этикетки (файлами или архивом)."
        )
        return
    pending = ctx.chat_data.pop("pending_jobs", None)
    if pending:
        await build_and_send(update, pending, pool, pool.name, [])


async def cmd_template(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    with tempfile.TemporaryDirectory() as td:
        path = await asyncio.to_thread(make_template, Path(td) / "Шаблон заказа ЧЗ.xlsx")
        with open(path, "rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=path.name,
                caption="Заполните артикулы по одному в строке — порядок строк станет "
                        "порядком страниц в PDF. Количество и маркетплейс необязательны: "
                        "повтор можно задать и просто второй строкой.\n"
                        "Свой файл тоже подойдёт — колонку с артикулами бот найдёт сам.",
            )


async def cmd_diag(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Состояние машины, на которой крутится бот."""
    if not is_allowed(update):
        await deny(update)
        return
    import platform

    tools = available_tools()
    root = pool_store.ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        storage = f"{root} — запись работает"
    except OSError as e:
        storage = f"{root} — ЗАПИСЬ НЕ РАБОТАЕТ: {e}"
    pools = pool_store.list_pools(update.effective_chat.id)
    await update.message.reply_text(
        "Python " + platform.python_version() + "\n"
        + "распаковщики: " + (", ".join(tools) if tools else "НЕТ НИ ОДНОГО (zip всё равно работает)")
        + "\nхранилище: " + storage
        + f"\nпулов в этом чате: {len(pools)}"
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    pool_store.clear_inbox(update.effective_chat.id)
    ctx.chat_data.clear()
    await update.message.reply_text("Накопленные файлы и незавершённые списки забыты. "
                                    "Сохранённые пулы остались: /pools")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    text = update.message.text or ""
    parsed = parse_message(text)

    if parsed.is_empty:
        pending = ctx.chat_data.get("pending_jobs")
        if pending and text.strip():
            ctx.chat_data["pool_name"] = text.strip()
        await update.message.reply_text("Не нашёл в сообщении ни одного артикула.\n\n" + HELP)
        return

    jobs = jobs_from(parsed)

    source, source_name, notes = await active_source(update, ctx, parsed.folder_query)
    if source is None:
        if parsed.folder_query:
            return  # про папку уже ответили
        ctx.chat_data["pending_jobs"] = jobs
        await update.message.reply_text(
            "Список принял: "
            + ", ".join(f"{j.marketplace} — {len(j.codes)} поз." for j in jobs)
            + ".\nТеперь пришлите PDF-этикетки (можно все сразу) или .zip — соберу PDF для печати."
        )
        return

    await build_and_send(update, jobs, source, source_name, notes + parsed.warnings)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await deny(update)
        return
    doc = update.message.document
    name = doc.file_name or "file"
    suffix = Path(name).suffix.lower()
    chat_id = update.effective_chat.id
    caption = (update.message.caption or "").strip()

    if suffix in ARCHIVE_SUFFIXES:
        with tempfile.TemporaryDirectory() as td:
            zpath = Path(td) / name
            if not await download_doc(update, doc, zpath):
                return
            try:
                sources = await asyncio.to_thread(collect_from_archive, zpath, Path(td) / "x")
            except ArchiveError as e:
                await update.message.reply_text(
                    f"Не смог распаковать «{name}»: {e}\n"
                    "Пришлите этикетки .zip-архивом или отдельными файлами."
                )
                return
            if not sources:
                await update.message.reply_text("В архиве нет PDF-файлов.")
                return
            pool_name = pool_name_from(caption, Path(name).stem)
            pool, unreadable = await asyncio.to_thread(
                pool_store.save_pool, chat_id, sources, pool_name
            )
        msg = f"Пул «{pool.name}» из архива: файлов {len(pool.filenames)}, страниц {pool.page_count}."
        if unreadable:
            msg += "\nНе удалось прочитать: " + ", ".join(unreadable)
        await update.message.reply_text(msg)

        # Список мог приехать в подписи к этому же архиву или раньше отдельным сообщением
        parsed_cap = parse_message(caption)
        jobs = jobs_from(parsed_cap) or ctx.chat_data.pop("pending_jobs", None)
        if jobs:
            await build_and_send(update, jobs, pool, pool.name, parsed_cap.warnings)
        else:
            await send_pool_file(update, pool)
            await update.message.reply_text("Теперь пришлите список артикулов.")
        return

    if suffix == ".pdf":
        inbox = pool_store.inbox_dir(chat_id)
        target = inbox / name
        n = 1
        while target.exists():
            target = inbox / f"{Path(name).stem}~{n}.pdf"
            n += 1
        if not await download_doc(update, doc, target):
            return

        # Это ранее собранный пул? Тогда просто делаем его активным.
        existing = await asyncio.to_thread(load_pool, target, Path(name).stem)
        if existing is not None:
            adopted = await asyncio.to_thread(
                pool_store.adopt_pool_file, chat_id, target,
                pool_name_from(caption, Path(name).stem),
            )
            target.unlink(missing_ok=True)
            if adopted:
                await update.message.reply_text(
                    f"Принял готовый пул «{adopted.name}»: файлов {len(adopted.filenames)}, "
                    f"страниц {adopted.page_count}. Присылайте список."
                )
                parsed_cap = parse_message(caption)
                jobs = jobs_from(parsed_cap) or ctx.chat_data.pop("pending_jobs", None)
                if jobs:
                    await build_and_send(update, jobs, adopted, adopted.name, parsed_cap.warnings)
                return

        if caption:
            parsed_cap = parse_message(caption)
            cap_jobs = jobs_from(parsed_cap)
            if cap_jobs:
                ctx.chat_data["pending_jobs"] = cap_jobs
            else:
                ctx.chat_data["pool_name"] = pool_name_from(caption, "")
        count = len(pool_store.inbox_files(chat_id))
        if count == 1:
            await update.message.reply_text("Принимаю этикетки… пришлите все файлы, дальше — список.")
        schedule_finalize(update, ctx)
        return

    if suffix in (".csv", ".xlsx", ".xlsm"):
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / name
            if not await download_doc(update, doc, local):
                return
            order = await asyncio.to_thread(parse_order_file, local)
        if order.warnings:
            await update.message.reply_text("\n".join(order.warnings))
        if not order.total:
            return

        # Маркетплейс: из колонки/маркера в файле, иначе из подписи, иначе нейтрально
        low = caption.lower()
        caption_market = ""
        if any(m in low for m in ("yandex", "яндекс", "ym")):
            caption_market = "YANDEX"
        elif any(m in low for m in ("ozon", "озон", "ozn")):
            caption_market = "OZON"

        jobs: list[Job] = []
        for market, codes in order.lists.items():
            label = market or caption_market or "ZAKAZ"
            for job in jobs:
                if job.marketplace == label:
                    job.codes.extend(codes)
                    break
            else:
                jobs.append(Job(label, codes))

        await update.message.reply_text(
            f"Из файла «{name}»: {order.note}.\n"
            + ", ".join(f"{j.marketplace.lower()} — {len(j.codes)} поз." for j in jobs)
            + "\nПорядок строк сохранён, повторы учтены."
        )

        parsed_cap = parse_message(caption)
        source, source_name, notes = await active_source(update, ctx, parsed_cap.folder_query)
        if source is None:
            ctx.chat_data["pending_jobs"] = jobs
            await update.message.reply_text(
                "Теперь пришлите этикетки — файлами или архивом."
            )
            return
        await build_and_send(update, jobs, source, source_name, notes)
        return

    await update.message.reply_text(
        "Понимаю: .pdf (этикетки или готовый пул), архив с этикетками "
        "(.zip, .rar, .7z, .tar), .xlsx / .csv со списком заказа (/template)."
    )


def get_token() -> str:
    token = os.environ.get("CZ_BOT_TOKEN", "").strip()
    if not token and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    return token


def preflight() -> str:
    """Понятная диагностика при старте — иначе в логах Railway просто «crashed»."""
    token = get_token()
    if not token:
        log.error(
            "НЕ ЗАДАН ТОКЕН БОТА. На Railway: Variables -> CZ_BOT_TOKEN = токен от BotFather "
            "(локально — файл %s). Без него бот запуститься не может.", TOKEN_FILE
        )
        raise SystemExit(1)
    if ":" not in token or not token.split(":", 1)[0].isdigit():
        log.error("CZ_BOT_TOKEN не похож на токен BotFather (ожидается вид 123456789:AA...). "
                  "Проверьте, что не скопировались кавычки или пробелы.")
        raise SystemExit(1)

    if not allowed_users():
        log.warning(
            "НЕ ЗАДАН СПИСОК ПОЛЬЗОВАТЕЛЕЙ (CZ_BOT_ALLOWED_USERS). Бот запустится, но будет "
            "отказывать всем: напишите ему /whoami и впишите свой ID в переменную."
        )
    if os.environ.get("CZ_POOL_ROOT"):
        log.info("Хранилище пулов: %s (том должен быть примонтирован, иначе пулы пропадут "
                 "при следующем деплое)", pool_store.ROOT)
    try:
        pool_store.ROOT.mkdir(parents=True, exist_ok=True)
        probe = pool_store.ROOT / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        log.error("Каталог для пулов %s недоступен на запись: %s. "
                  "На Railway добавьте Volume с точкой монтирования /data и CZ_POOL_ROOT=/data/pools.",
                  pool_store.ROOT, e)
        raise SystemExit(1)
    return token


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Понятная строка в логах вместо простыни трейсбека."""
    err = ctx.error
    if isinstance(err, Conflict):
        log.error(
            "ЭТОГО ЖЕ БОТА СЛУШАЕТ ДРУГОЙ ПРОЦЕСС. Telegram отдаёт обновления только одному: "
            "остановите локальный ./run_bot.sh или лишний сервис — иначе ответы будут теряться."
        )
        return
    log.exception("Необработанная ошибка", exc_info=err)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(f"Что-то пошло не так: {err}")
        except TelegramError:
            pass


def main() -> None:
    app = Application.builder().token(preflight()).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("pools", cmd_pools))
    app.add_handler(CommandHandler("pool", cmd_pool))
    app.add_handler(CommandHandler("go", cmd_go))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CommandHandler("template", cmd_template))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    tools = available_tools()
    log.info("Бот запущен. Хранилище пулов: %s | распаковщики: %s",
             pool_store.ROOT, ", ".join(tools) if tools else "нет (zip обрабатывается своими силами)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
