import asyncio
import io
import os
import sqlite3
from datetime import datetime

from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

WEBAPP_URL = ""


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎭 Выбрать места", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text(
        "👋 Привет! Это бронирование мест на «Визитку 2026».\n\n"
        "📅 25 ноября, 18:00 — Дворец культуры железнодорожников (ул. Чкалова, 7)\n\n"
        "Нажми кнопку ниже — откроется схема зала.\n\n"
        "📱 Для просмотра полной рассадки разверни экран горизонтально.\n\n"
        "⚠️ При бронировании укажи корректные ФИО, курс и группу — по ним "
        "идёт сверка на входе. С неверными данными не пропустят.\n\n"
        "🎟 Если ты не студент — отметь в форме «Я гость»: курс и группу "
        "заполнять не нужно, бронирование идёт по ФИО.\n\n"
        "🎟 Билеты сверяются на входе по списку — держи их под рукой до мероприятия.",
        reply_markup=kb,
    )


def _save_ticket_msg(uid: int, section: str, row: str, seat: int, chat_id: int, message_id: int) -> None:
    """Запоминает сообщение с билетом, чтобы при отмене брони его можно было удалить."""
    db_path = os.getenv("DB_PATH", "seats.db")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        conn.execute(
            "INSERT INTO ticket_msgs(user_id,section,row,seat,chat_id,message_id,ts) "
            "VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(section,row,seat) DO UPDATE SET "
            "user_id=excluded.user_id, chat_id=excluded.chat_id, "
            "message_id=excluded.message_id, ts=excluded.ts",
            (uid, section, row, seat, chat_id, message_id, datetime.utcnow().isoformat()),
        )
    finally:
        conn.close()


async def tickets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Повторно присылает билеты на все места гостя (по одному файлу на место)."""
    from ticket import make_ticket
    from main import ticket_caption
    conn = sqlite3.connect(os.getenv("DB_PATH", "seats.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT section, row, seat FROM seats WHERE user_id=? "
        "ORDER BY section DESC, CAST(row AS INTEGER), seat",
        (update.effective_user.id,)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("У вас пока нет забронированных мест.")
        return
    for r in rows:
        section, row, seat = r["section"], r["row"], r["seat"]
        data, name = await asyncio.to_thread(make_ticket, row, seat, section=section)
        msg = await update.message.reply_document(
            document=io.BytesIO(data), filename=name,
            caption=ticket_caption(section, row, seat),
        )
        try:
            _save_ticket_msg(update.effective_user.id, section, row, seat,
                             msg.chat_id, msg.message_id)
        except Exception as e:
            print(f"TICKET MSG SAVE SKIP: {type(e).__name__}: {e}")


async def resend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Админ-команда: удаляет старые балконные билеты и рассылает их заново
    в новом формате (с плашкой «БАЛКОН») только тем, у кого есть балконная бронь."""
    print("DEBUG: Команда /resend получена!")
    admin_ids = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("Нет доступа")
        return

    from ticket import make_ticket
    from main import ticket_caption
    from telegram import Bot

    conn = sqlite3.connect(os.getenv("DB_PATH", "seats.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT user_id, section, row, seat FROM seats "
        "WHERE section='balcony' "
        "ORDER BY user_id, CAST(row AS INTEGER), seat"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Балконных броней нет — рассылать нечего.")
        return

    by_user = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r)

    await update.message.reply_text(
        f"Балконных бронировщиков: {len(by_user)}. "
        "Удаляю старые балконные билеты и рассылаю новые…"
    )

    ok_users, fail_users = 0, []
    async with Bot(os.environ["BOT_TOKEN"]) as bot:
        for uid, bookings in by_user.items():
            try:
                # 1) удаляем старые сообщения с БАЛКОННЫМИ билетами (партерные не трогаем)
                conn = sqlite3.connect(os.getenv("DB_PATH", "seats.db"))
                conn.row_factory = sqlite3.Row
                old = conn.execute(
                    "SELECT chat_id, message_id FROM ticket_msgs "
                    "WHERE user_id=? AND section='balcony'",
                    (uid,)
                ).fetchall()
                conn.close()
                for m in old:
                    try:
                        await bot.delete_message(chat_id=m["chat_id"],
                                                 message_id=m["message_id"])
                    except Exception as e:
                        # чаще всего: сообщение старше 48 часов или уже удалено вручную
                        print(f"OLD DELETE SKIP user={uid}: {type(e).__name__}: {e}")

                # 2) шлём новые балконные билеты с плашкой
                for r in bookings:
                    section, row, seat = r["section"], r["row"], r["seat"]
                    data, name = await asyncio.to_thread(
                        make_ticket, row, seat, section=section)
                    msg = await bot.send_document(
                        chat_id=uid,
                        document=io.BytesIO(data),
                        filename=name,
                        caption=ticket_caption(section, row, seat),
                    )
                    try:
                        _save_ticket_msg(uid, section, row, seat,
                                         msg.chat_id, msg.message_id)
                    except Exception as e:
                        print(f"TICKET MSG SAVE SKIP: {type(e).__name__}: {e}")
                ok_users += 1
            except Exception as e:
                print(f"RESEND FAIL user={uid}: {type(e).__name__}: {e}")
                fail_users.append(uid)

    report = (f"Готово: новые балконные билеты отправлены "
              f"{ok_users} пользователю(ям).")
    if fail_users:
        report += f"\nНе удалось отправить: {', '.join(map(str, fail_users))} " \
                  "(бот не может писать тем, кто не начинал с ним чат)."
    await update.message.reply_text(report)


async def fix29(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Админ-команда: пересаживает тех, кто забронировал несуществующее место 29
    в рядах 6–11, на свободные места, пишет каждому объяснение, удаляет старые
    билеты и присылает новые."""
    print("DEBUG: Команда /fix29 получена!")
    admin_ids = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("Нет доступа")
        return

    from main import (migrate_parter_29, delete_ticket_msgs, send_tickets)
    from telegram import Bot

    try:
        moved = migrate_parter_29()
    except Exception as e:
        import traceback
        traceback.print_exc()
        await update.message.reply_text(f"Ошибка миграции: {type(e).__name__}: {e}")
        return

    if not moved:
        await update.message.reply_text("Броней на место 29 (ряды 6–11) не найдено.")
        return

    await update.message.reply_text(f"Найдено пересадок: {len(moved)}. Рассылаю…")

    ok, fail = 0, []
    async with Bot(os.environ["BOT_TOKEN"]) as bot:
        for m in moved:
            try:
                await bot.send_message(
                    m["user_id"],
                    "⚠️ Произошла техническая ошибка: место 29 в ряду "
                    f"{m['old_row']} в зале фактически не существует, "
                    "и забронировать его было нельзя.\n\n"
                    f"Вам автоматически выделено другое место: "
                    f"ряд {m['new_row']}, место {m['new_seat']}.\n"
                    "Старые билеты недействительны — сейчас пришлём новые. "
                    "Приносим извинения за неудобства!",
                )
            except Exception as e:
                print(f"FIX29 NOTICE SKIP user={m['user_id']}: {type(e).__name__}: {e}")
            try:
                await delete_ticket_msgs(m["user_id"], "parter", m["old_row"], [29])
            except Exception as e:
                print(f"FIX29 DELETE FAIL user={m['user_id']}: {type(e).__name__}: {e}")
            try:
                await send_tickets(m["user_id"], "parter", m["new_row"], [m["new_seat"]])
                ok += 1
            except Exception as e:
                print(f"FIX29 SEND FAIL user={m['user_id']}: {type(e).__name__}: {e}")
                fail.append(m["user_id"])

    report = f"Готово: пересажено и уведомлено {ok}."
    if fail:
        report += f"\nПроблемы у: {', '.join(map(str, set(fail)))}."
    await update.message.reply_text(report)


async def export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    print("DEBUG: Команда /export получена!")
    admin_ids = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("Нет доступа")
        return

    try:
        from main import PARTER_ROWS, GROUP_ROWS_PARTER, BALCONY_ROWS
        from export_excel import build_xlsx

        buf = build_xlsx(PARTER_ROWS, GROUP_ROWS_PARTER, BALCONY_ROWS)
        await update.message.reply_document(document=buf, filename="seats.xlsx")
    except Exception as e:
        print(f"DEBUG: ОШИБКА ПРИ ЭКСПОРТЕ: {type(e).__name__}: {e}")
        await update.message.reply_text(f"Ошибка экспорта: {e}")


async def run_bot_async(token: str, webapp_url: str):
    global WEBAPP_URL
    WEBAPP_URL = webapp_url
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tickets", tickets))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("resend", resend))
    app.add_handler(CommandHandler("fix29", fix29))
    async with app:
        await app.start()
        await app.updater.start_polling()
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
