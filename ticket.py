"""Генерация билета: ряд и место вписываются в белые поля макета."""
import io, json, os, zlib
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickets")
META = json.load(open(os.path.join(BASE, "meta.json"), encoding="utf-8"))
FONT = os.path.join(BASE, "font.ttf")   # можно заменить на шрифт макета (жирный, с цифрами)
INK = (26, 18, 16)                       # цвет цифр (почти чёрный)
SCALE = 2                                # макет 300 dpi -> печать 600 dpi
FMT = os.getenv("TICKET_FORMAT", "pdf")  # pdf | png


def _fit(draw, text, w, h):
    size = int(h * 0.95)
    while size > 10:
        f = ImageFont.truetype(FONT, size)
        l, t, r, b = draw.textbbox((0, 0), text, font=f)
        if r - l <= w * 0.82:
            return f
        size -= 2
    return ImageFont.truetype(FONT, 10)


def make_ticket(row, seat, fmt=None):
    """Возвращает (bytes, имя_файла). Макет выбирается по месту, поэтому
    повторная отправка даёт тот же самый билет."""
    fmt = fmt or FMT
    m = META[zlib.crc32(f"{row}-{seat}".encode()) % len(META)]
    img = Image.open(os.path.join(BASE, m["file"])).convert("RGB")
    img = img.resize((img.width * SCALE, img.height * SCALE), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    for box, text in zip(m["boxes"], (str(row), str(seat))):
        x0, y0, x1, y1 = [v * SCALE for v in box]
        f = _fit(d, text, x1 - x0, y1 - y0)
        d.text(((x0 + x1) / 2, (y0 + y1) / 2), text, font=f, fill=INK, anchor="mm")
    buf = io.BytesIO()
    name = f"bilet_ryad{row}_mesto{seat}"
    if fmt == "png":
        img.save(buf, "PNG", dpi=(300 * SCALE, 300 * SCALE))
        return buf.getvalue(), name + ".png"
    img.save(buf, "PDF", resolution=300.0 * SCALE)   # физический размер ≈ 14×5 см
    return buf.getvalue(), name + ".pdf"
