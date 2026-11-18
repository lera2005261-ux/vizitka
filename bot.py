import asyncio
import io
import os
import sqlite3
from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

WEBAPP_URL = ""


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎭 Выбрать места", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text("Нажмите кнопку, чтобы выбрать места:", reply_markup=kb)


async def tickets(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Повторно присылает билеты на все места гостя (по одному файлу на место)."""
    from ticket import make_ticket
    conn = sqlite3.connect(os.getenv("DB_PATH", "seats.db"))
    rows = conn.execute(
        "SELECT row, seat FROM seats WHERE user_id=? ORDER BY CAST(row AS INTEGER), seat",
        (update.effective_user.id,)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("У вас пока нет забронированных мест.")
        return
    for row, seat in rows:
        data, name = await asyncio.to_thread(make_ticket, row, seat)
        await update.message.reply_document(
            document=io.BytesIO(data), filename=name,
            caption=f"🎟 Ряд {row}, место {seat}\nПечатайте в масштабе 100% (без «по размеру страницы»)."
        )


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
