import os
import io
import json
import hmac
import hashlib
import time
import sqlite3
import asyncio
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000").strip()
ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
MAX_SEATS = int(os.getenv("MAX_SEATS", "4"))
DB_PATH = os.getenv("DB_PATH", "seats.db")
# Проверка подписи Telegram. Для отладки можно временно поставить VERIFY_INIT=0
VERIFY_INIT = os.getenv("VERIFY_INIT", "1").strip() != "0"

TEST_MODE = not BOT_TOKEN


def ticket_caption(section: str, row: str, seat: int) -> str:
    """Подпись к билету: для балкона помечаем секцию."""
    place = f"Балкон, ряд {row}, место {seat}" if section == "balcony" else f"Ряд {row}, место {seat}"
    return (
        f"🎟 {place}\n"
        "Сверка на входе по списку: данные в брони (ФИО, курс, группа) "
        "должны быть корректными — иначе не пропустят.\n"
        "Печать по желанию: в масштабе 100% (без «по размеру страницы»)."
    )

# ----------------------------------------------------------------------------
# Схема зала
# ----------------------------------------------------------------------------
PARTER_ROWS = {
    "1":  list(range(21, 0, -1)),   # 21 место
    "2":  list(range(23, 0, -1)),   # 23 места
    "3":  list(range(23, 0, -1)),   # 23 места
    "4":  list(range(24, 0, -1)),   # 24 места
    "5":  list(range(25, 0, -1)),   # 25 мест
    "6":  list(range(28, 0, -1)),   # места 29 не существует — схема исправлена
    "7":  list(range(28, 0, -1)),
    "8":  list(range(28, 0, -1)),
    "9":  list(range(28, 0, -1)),
    "10": list(range(28, 0, -1)),
    "11": list(range(28, 0, -1)),
    "12": list(range(26, 0, -1)),   # ряд 12 открыт для бронирования (26 мест)
}

GROUP_ROWS_PARTER = {
    "13": list(range(30, 0, -1)),   # 30 мест
    "14": list(range(30, 0, -1)),   # 30 мест
    "15": list(range(30, 15, -1)) + list(range(15, 0, -1)),   # проход между 16 и 14
    "16": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "17": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "18": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "19": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "20": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "21": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "22": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "23": list(range(26, 0, -1)),   # весь ряд — группы: 1–4 салатовые, 5–24 зелёные, 25–26 синие
}

BALCONY_ROWS = {
    "1": [10, 9, 8, 7, 6, 5, 4, 3, 2, 1],
    "2": [30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1],
    "3": [29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1],
    "4": [29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1],
    "5": [28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1],
    "6": [29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 19, 18, 17, 16, 15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1],
}

# ----------------------------------------------------------------------------
# База данных
# ----------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db() -> None:
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS seats (
            section TEXT NOT NULL,
            row     TEXT NOT NULL,
            seat    INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            fio     TEXT NOT NULL,
            course  TEXT NOT NULL,
            grp     TEXT NOT NULL,
            ts      TEXT NOT NULL,
            PRIMARY KEY (section, row, seat)
        );
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            fio     TEXT NOT NULL,
            course  TEXT NOT NULL,
            grp     TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ticket_msgs (
            user_id    INTEGER NOT NULL,
            section    TEXT NOT NULL,
            row        TEXT NOT NULL,
            seat       INTEGER NOT NULL,
            chat_id    INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            ts         TEXT NOT NULL,
            PRIMARY KEY (section, row, seat)
        );
        """)

# Места на балконе, доступные для обычного бронирования
BOOKABLE_BALCONY = {
    "2": set(range(26, 30)),  # ряд 2, места 26–29
}

def seat_is_bookable(section: str, row: str, seat: int) -> bool:
    if section == "balcony":
        return seat in BOOKABLE_BALCONY.get(row, set())
    if section == "parter":
        if row in GROUP_ROWS_PARTER and seat in GROUP_ROWS_PARTER[row]:
            return False   # это конкретное место закреплено за группой
        if row not in PARTER_ROWS:
            return False
        return seat in PARTER_ROWS[row]
    return False

# ----------------------------------------------------------------------------
# Миграция: места 29 в рядах 6–11 не существуют — пересаживаем бронировавших.
# Запускается вручную админ-командой /fix29 (НЕ при старте приложения!)
# ----------------------------------------------------------------------------
def _find_free_parter_seat(conn: sqlite3.Connection, orig_row: str, orig_seat: int):
    """Ищет свободное место: сначала тот же ряд (максимальный номер —
    ближе к прежнему месту), затем соседние ряды."""
    rows_order = sorted(PARTER_ROWS.keys(), key=lambda r: abs(int(r) - int(orig_row)))
    for row in rows_order:
        for s in PARTER_ROWS[row]:  # списки уже идут от большего к меньшему
            if row == orig_row and s == orig_seat:
                continue
            taken = conn.execute(
                "SELECT 1 FROM seats WHERE section='parter' AND row=? AND seat=?",
                (row, s),
            ).fetchone()
            if not taken:
                return row, s
    return None


def migrate_parter_29() -> list:
    """Пересаживает всех, кто забронировал место 29 в партере рядов 6–11.
    Возвращает список пересадок для рассылки новых билетов."""
    moved = []
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT user_id, section, row, seat, fio, course, grp, ts FROM seats "
                "WHERE section='parter' AND seat=29 "
                "AND CAST(row AS INTEGER) IN (6,7,8,9,10,11)"
            ).fetchall()
            for r in rows:
                spot = _find_free_parter_seat(conn, r["row"], r["seat"])
                if spot is None:
                    print(f"SEAT 29 MIGRATION: не нашлось свободного места для "
                          f"user={r['user_id']} (ряд {r['row']}) — бронь оставлена как есть")
                    continue
                new_row, new_seat = spot
                conn.execute(
                    "DELETE FROM seats WHERE section='parter' AND row=? AND seat=?",
                    (r["row"], r["seat"]),
                )
                conn.execute(
                    "INSERT INTO seats(section,row,seat,user_id,fio,course,grp,ts) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    ("parter", new_row, new_seat, r["user_id"], r["fio"],
                     r["course"], r["grp"], r["ts"]),
                )
                moved.append({"user_id": r["user_id"], "old_row": r["row"],
                              "new_row": new_row, "new_seat": new_seat})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return moved

# ----------------------------------------------------------------------------
# Проверка пользователя Telegram (с проверкой подписи initData)
# ----------------------------------------------------------------------------
def current_user(request: Request) -> Optional[dict]:
    init_data = request.headers.get("X-Telegram-Init-Data", "")

    if not init_data:
        print("AUTH FAIL: initData is EMPTY")
        return None

    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))

        if VERIFY_INIT and BOT_TOKEN:
            got_hash = pairs.pop("hash", "")
            check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
            secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
            expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, got_hash):
                print("AUTH FAIL: bad signature (проверьте, что BOT_TOKEN от того же бота)")
                return None
            if time.time() - int(pairs.get("auth_date", "0")) > 86400:
                print("AUTH FAIL: initData is older than 24h")
                return None

        user = json.loads(pairs.get("user", "{}"))
        if not user or "id" not in user:
            print("AUTH FAIL: user field is empty")
            return None
        print(f"AUTH SUCCESS: user_id={user.get('id')}")
        return user
    except Exception as e:
        print(f"AUTH FAIL: Parse error {type(e).__name__}: {e}")
        return None

# ----------------------------------------------------------------------------
# Отправка и удаление билетов в Telegram
# ----------------------------------------------------------------------------
async def send_tickets(uid: int, section: str, row: str, seats: list) -> None:
    try:
        from telegram import Bot
        from ticket import make_ticket
        async with Bot(BOT_TOKEN) as bot:
            for s in sorted(seats):
                data, name = await asyncio.to_thread(make_ticket, row, s, section=section)
                msg = await bot.send_document(
                    chat_id=uid,
                    document=io.BytesIO(data),
                    filename=name,
                    caption=ticket_caption(section, row, s),
                )
                # запоминаем сообщение с билетом, чтобы удалить его при отмене брони
                with db() as conn:
                    conn.execute(
                        "INSERT INTO ticket_msgs(user_id,section,row,seat,chat_id,message_id,ts) "
                        "VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(section,row,seat) DO UPDATE SET "
                        "user_id=excluded.user_id, chat_id=excluded.chat_id, "
                        "message_id=excluded.message_id, ts=excluded.ts",
                        (uid, section, row, s, uid, msg.message_id,
                         datetime.utcnow().isoformat()),
                    )
        print(f"TICKETS OK: user={uid} section={section} row={row} seats={seats}")
    except Exception as e:
        # бронь уже сохранена; билеты можно получить командой /tickets
        print(f"TICKET SEND FAIL: {type(e).__name__}: {e}")


async def delete_ticket_msgs(uid: int, section: str, row: str, seats: list) -> None:
    """Удаляет сообщения с билетами из чата после отмены брони."""
    from telegram import Bot
    msgs = []
    with db() as conn:
        for s in seats:
            r = conn.execute(
                "SELECT chat_id, message_id FROM ticket_msgs "
                "WHERE section=? AND row=? AND seat=? AND user_id=?",
                (section, row, s, uid),
            ).fetchone()
            if r:
                msgs.append((r["chat_id"], r["message_id"]))
                conn.execute(
                    "DELETE FROM ticket_msgs WHERE section=? AND row=? AND seat=?",
                    (section, row, s),
                )
    if not msgs:
        return
    try:
        async with Bot(BOT_TOKEN) as bot:
            for chat_id, message_id in msgs:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                except Exception as e:
                    # чаще всего: сообщение старше 48 часов или уже удалено вручную
                    print(f"TICKET DELETE SKIP: {type(e).__name__}: {e}")
        print(f"TICKET MESSAGES DELETED: user={uid} row={row} seats={seats}")
    except Exception as e:
        print(f"TICKET DELETE FAIL: {type(e).__name__}: {e}")

# ----------------------------------------------------------------------------
# Модели
# ----------------------------------------------------------------------------
class ProfileIn(BaseModel):
    fio: str
    course: str = ""
    grp: str = ""
    is_guest: bool = False

class BookIn(BaseModel):
    section: str
    row: str
    seats: list[int]

# ----------------------------------------------------------------------------
# FastAPI
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    if BOT_TOKEN:
        from bot import run_bot_async
        app.state.bot_task = asyncio.create_task(run_bot_async(BOT_TOKEN, WEBAPP_URL))
    yield
    task = getattr(app.state, "bot_task", None)
    if task:
        task.cancel()

app = FastAPI(lifespan=lifespan)

# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
@app.get("/api/state")
def api_state(request: Request):
    user = current_user(request)
    uid = user["id"] if user else None

    seats = []
    with db() as conn:
        rows = conn.execute("SELECT section,row,seat,user_id FROM seats").fetchall()
    busy = {(r["section"], r["row"], r["seat"]): r["user_id"] for r in rows}

    for row, seats_list in PARTER_ROWS.items():
        for s in seats_list:
            key = ("parter", row, s)
            if key in busy:
                seats.append({"section": "parter", "row": row, "seat": s,
                              "status": "mine" if busy[key] == uid else "busy"})
            else:
                seats.append({"section": "parter", "row": row, "seat": s, "status": "free"})

    for row, seats_list in GROUP_ROWS_PARTER.items():
        for s in seats_list:
            seats.append({"section": "parter", "row": row, "seat": s, "status": "group"})

    for row, seats_list in BALCONY_ROWS.items():
        bookable = BOOKABLE_BALCONY.get(row, set())
        for s in seats_list:
            if s in bookable:
                key = ("balcony", row, s)
                if key in busy:
                    seats.append({"section": "balcony", "row": row, "seat": s,
                                  "status": "mine" if busy[key] == uid else "busy"})
                else:
                    seats.append({"section": "balcony", "row": row, "seat": s, "status": "free"})
            else:
                seats.append({"section": "balcony", "row": row, "seat": s, "status": "group"})

    return {"seats": seats, "max_seats": MAX_SEATS, "user_id": uid, "test_mode": TEST_MODE}

@app.get("/api/profile")
def api_profile(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Не авторизован")
    with db() as conn:
        r = conn.execute("SELECT fio,course,grp FROM profiles WHERE user_id=?",
                         (user["id"],)).fetchone()
    return dict(r) if r else {}

@app.post("/api/profile")
def api_save_profile(request: Request, p: ProfileIn):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Не авторизован")
    if not p.fio.strip():
        raise HTTPException(400, "Укажите ФИО")
    if p.is_guest:
        course, grp = "гость", "гость"
    else:
        course, grp = p.course.strip(), p.grp.strip()
        if not course or not grp:
            raise HTTPException(400, "Заполните курс и группу (или отметьте «Я гость»)")
    with db() as conn:
        conn.execute(
            "INSERT INTO profiles(user_id,fio,course,grp) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET fio=excluded.fio,"
            "course=excluded.course,grp=excluded.grp",
            (user["id"], p.fio.strip(), course, grp),
        )
    return {"ok": True}

@app.post("/api/book")
def api_book(request: Request, b: BookIn, background: BackgroundTasks):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Не авторизован")
    uid = user["id"]

    with db() as conn:
        prof = conn.execute("SELECT fio,course,grp FROM profiles WHERE user_id=?",
                            (uid,)).fetchone()
    if not prof:
        raise HTTPException(400, "Сначала заполните ФИО, курс и группу")

    with db() as conn:
        cnt = conn.execute("SELECT COUNT(*) c FROM seats WHERE user_id=?", (uid,)).fetchone()["c"]
    if cnt + len(b.seats) > MAX_SEATS:
        raise HTTPException(400, f"Не больше {MAX_SEATS} мест на аккаунт")

    for s in b.seats:
        if not seat_is_bookable(b.section, b.row, s):
            raise HTTPException(400, f"Место {s} закреплено за группой")

    taken = []
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for s in b.seats:
                try:
                    conn.execute(
                        "INSERT INTO seats(section,row,seat,user_id,fio,course,grp,ts) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (b.section, b.row, s, uid, prof["fio"], prof["course"],
                         prof["grp"], datetime.utcnow().isoformat()),
                    )
                except sqlite3.IntegrityError:
                    taken.append(s)
            if taken:
                conn.execute("ROLLBACK")
            else:
                conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if taken:
        raise HTTPException(409, f"Уже заняты: {', '.join(map(str, taken))}")

    # бронь сохранена -> отправляем билеты (по одному файлу на место)
    if BOT_TOKEN:
        background.add_task(send_tickets, uid, b.section, b.row, list(b.seats))
    return {"ok": True}

@app.delete("/api/book")
def api_cancel(request: Request, b: BookIn, background: BackgroundTasks):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Не авторизован")
    uid = user["id"]

    deleted = []
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for s in b.seats:
                cur = conn.execute(
                    "DELETE FROM seats WHERE section=? AND row=? AND seat=? AND user_id=?",
                    (b.section, b.row, s, uid)
                )
                if cur.rowcount > 0:
                    deleted.append(s)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    if not deleted:
        raise HTTPException(400, "Эти места вам не принадлежат или уже отменены")

    # бронь отменена -> удаляем сообщения с билетами из чата
    if BOT_TOKEN:
        background.add_task(delete_ticket_msgs, uid, b.section, b.row, list(deleted))
    return {"ok": True, "deleted": deleted}

@app.get("/api/export")
def api_export(key: str = Query(...)):
    if key != ADMIN_KEY:
        raise HTTPException(403, "Неверный ключ")
    from export_excel import build_xlsx
    buf = build_xlsx(PARTER_ROWS, GROUP_ROWS_PARTER, BALCONY_ROWS)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="seats.xlsx"'},
    )

@app.get("/", response_class=HTMLResponse)
def index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

app.mount("/static", StaticFiles(directory="static"), name="static")
