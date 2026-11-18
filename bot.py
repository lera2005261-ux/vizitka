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


async def export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    print("DEBUG: Команда /export получена!")
    admin_ids = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x}
    if update.effective_user.id not in admin_ids:
        print(f"DEBUG: Пользователь {update.effective_user.id} не админ. Админы: {admin_ids}")
        await update.message.reply_text("Нет доступа")
        return

    print("DEBUG: Пользователь админ, начинаю экспорт...")
    try:
        from main import PARTER_ROWS, GROUP_ROWS_PARTER, BALCONY_ROWS
        from export_excel import build_xlsx

        buf = build_xlsx(PARTER_ROWS, GROUP_ROWS_PARTER, BALCONY_ROWS)
        print("DEBUG: Excel-файл создан, отправляю...")
        await update.message.reply_document(document=buf, filename="seats.xlsx")
        print("DEBUG: Файл успешно отправлен!")
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
    async with app:
        await app.start()
        await app.updater.start_polling()
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
