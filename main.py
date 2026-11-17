import os
import json
import sqlite3
import asyncio
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Query
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

TEST_MODE = not BOT_TOKEN

# ----------------------------------------------------------------------------
# Схема зала
# ----------------------------------------------------------------------------
PARTER_ROWS = {
    "1":  list(range(21, 0, -1)),
    "2":  list(range(23, 0, -1)),
    "3":  list(range(23, 0, -1)),
    "4":  list(range(24, 0, -1)),
    "5":  list(range(25, 0, -1)),
    "6":  list(range(29, 0, -1)),
    "7":  list(range(29, 0, -1)),
    "8":  list(range(29, 0, -1)),
    "9":  list(range(29, 0, -1)),
    "10": list(range(29, 0, -1)),
    "11": list(range(29, 0, -1)),
}

GROUP_ROWS_PARTER = {
    "12": list(range(26, 14, -1)) + list(range(16, 0, -1)),
    "13": list(range(26, 14, -1)) + list(range(16, 0, -1)),
    "14": list(range(26, 14, -1)) + list(range(16, 0, -1)),
    "15": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "16": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "17": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "18": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "19": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "20": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "21": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "22": list(range(30, 15, -1)) + list(range(15, 0, -1)),
    "23": list(range(24, 14, -1)) + list(range(13, 0, -1)),
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
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
        """)

def seat_is_bookable(section: str, row: str, seat: int) -> bool:
    if section == "balcony":
        return False
    if section == "parter":
        if row in GROUP_ROWS_PARTER:
            return False
        if row not in PARTER_ROWS:
            return False
        return seat in PARTER_ROWS[row]
    return False

# ----------------------------------------------------------------------------
# ВРЕМЕННАЯ ПРОВЕРКА TELEGRAM (без подписи, только парсинг)
# ----------------------------------------------------------------------------
def current_user(request: Request) -> Optional[dict]:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    
    if not init_data:
        print("AUTH FAIL: initData is EMPTY")
        return None
        
    try:
        parsed = urllib.parse.parse_qs(init_data)
        user_json = parsed.get("user", ["{}"])[0]
        user = json.loads(user_json)
        if not user:
            print("AUTH FAIL: user field is empty")
            return None
        print(f"AUTH SUCCESS: user_id={user.get('id')}")
        return user
    except Exception as e:
        print(f"AUTH FAIL: Parse error {type(e).__name__}: {e}")
        return None

# ----------------------------------------------------------------------------
# Модели
# ----------------------------------------------------------------------------
class ProfileIn(BaseModel):
    fio: str
    course: str
    grp: str

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
        for s in seats_list:
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
    with db() as conn:
        conn.execute(
            "INSERT INTO profiles(user_id,fio,course,grp) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET fio=excluded.fio,"
            "course=excluded.course,grp=excluded.grp",
            (user["id"], p.fio.strip(), p.course.strip(), p.grp.strip()),
        )
    return {"ok": True}

@app.post("/api/book")
def api_book(request: Request, b: BookIn):
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
    return {"ok": True}

@app.delete("/api/book")
def api_cancel(request: Request, b: BookIn):
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
