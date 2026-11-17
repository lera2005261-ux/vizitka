import io
import os
import sqlite3
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font

GREEN = PatternFill("solid", fgColor="C6EFCE")
RED   = PatternFill("solid", fgColor="FFC7CE")
GRAY  = PatternFill("solid", fgColor="D9D9D9")

DB_PATH = os.getenv("DB_PATH", "seats.db")

def build_xlsx(PARTER_ROWS, GROUP_ROWS_PARTER, BALCONY_ROWS) -> io.BytesIO:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    booked = {(r["section"], r["row"], r["seat"]): r
              for r in conn.execute("SELECT * FROM seats")}
    conn.close()

    wb = Workbook()

    ws = wb.active
    ws.title = "Занятые"
    ws.append(["Секция", "Ряд", "Место", "ФИО", "Курс", "Группа", "Время"])
    for (sec, row, seat), r in sorted(booked.items()):
        ws.append([sec, row, seat, r["fio"], r["course"], r["grp"], r["ts"]])

    ws2 = wb.create_sheet("Все места")
    ws2.append(["Секция", "Ряд", "Место", "Статус"])
    for row, seats in PARTER_ROWS.items():
        for s in seats:
            key = ("parter", row, s)
            status = "занято" if key in booked else "свободно"
            ws2.append(["parter", row, s, status])
            ws2.cell(ws2.max_row, 4).fill = RED if status == "занято" else GREEN
    for row, seats in GROUP_ROWS_PARTER.items():
        for s in seats:
            ws2.append(["parter", row, s, "группа"])
            ws2.cell(ws2.max_row, 4).fill = GRAY
    for row, seats in BALCONY_ROWS.items():
        for s in seats:
            ws2.append(["balcony", row, s, "группа"])
            ws2.cell(ws2.max_row, 4).fill = GRAY

    ws3 = wb.create_sheet("Итого")
    ws3.append(["Показатель", "Значение"])
    ws3.append(["Гостевых мест партера", sum(len(v) for v in PARTER_ROWS.values())])
    ws3.append(["Занято", len(booked)])
    ws3.append(["Свободно", sum(len(v) for v in PARTER_ROWS.values()) - len(booked)])
    ws3.append(["Балкон (всего)", sum(len(v) for v in BALCONY_ROWS.values())])
    ws3.append(["Балкон гостевых", 0])
    ws3.append(["Балкон — только группы", "да"])
    for cell in ws3[1]:
        cell.font = Font(bold=True)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
