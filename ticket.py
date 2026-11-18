"""Генерация билета: ряд и место вписываются в белые поля макета.

Для балконных мест на билете ставится красная плашка «БАЛКОН»
в правом верхнем углу (make_ticket(..., section="balcony")).
"""
import io, json, os, zlib
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tickets")
META = json.load(open(os.path.join(BASE, "meta.json"), encoding="utf-8"))
FONT = os.path.join(BASE, "font.ttf")   # можно заменить на шрифт макета (жирный, с цифрами)
INK = (26, 18, 16)                       # цвет цифр (почти чёрный)
SCALE = 2                                # макет 300 dpi -> печать 600 dpi
FMT = os.getenv("TICKET_FORMAT", "pdf")  # pdf | png

BADGE_BG = (198, 40, 40)   # красный плашки
BADGE_FG = (255, 255, 255) # белый текст плашки


def _fit(draw, text, w, h):
    size = int(h * 0.95)
    while size > 10:
        f = ImageFont.truetype(FONT, size)
        l, t, r, b = draw.textbbox((0, 0), text, font=f)
        if r - l <= w * 0.82:
            return f
        size -= 2
    return ImageFont.truetype(FONT, 10)


def _fit_badge(draw, text, max_w, max_h):
    size = int(max_h)
    while size > 10:
        f = ImageFont.truetype(FONT, size)
        l, t, r, b = draw.textbbox((0, 0), text, font=f)
        if r - l <= max_w:
            return f
        size -= 2
    return ImageFont.truetype(FONT, 10)


def _stamp_section(img, d, section):
    """Рисует красную плашку «БАЛКОН» в правом верхнем углу билета."""
    if section != "balcony":
        return
    text = "БАЛКОН"
    pad = int(img.height * 0.035)
    max_text_h = int(img.height * 0.20)
    max_text_w = int(img.width * 0.88)
    f = _fit_badge(d, text, max_text_w, max_text_h)
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    tw, th = r - l, b - t
    bw, bh = tw + pad * 2, th + pad * 2
    x0 = img.width - bw - pad
    y0 = pad
    try:
        d.rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=bh // 5,
                            fill=BADGE_BG)
    except AttributeError:  # старые версии Pillow без rounded_rectangle
        d.rectangle([x0, y0, x0 + bw, y0 + bh], fill=BADGE_BG)
    d.text((x0 + bw / 2, y0 + bh / 2), text, font=f, fill=BADGE_FG, anchor="mm")


def make_ticket(row, seat, fmt=None, section="parter"):
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
    _stamp_section(img, d, section)
    buf = io.BytesIO()
    prefix = "bilet_balkon" if section == "balcony" else "bilet"
    name = f"{prefix}_ryad{row}_mesto{seat}"
    if fmt == "png":
        img.save(buf, "PNG", dpi=(300 * SCALE, 300 * SCALE))
        return buf.getvalue(), name + ".png"
    img.save(buf, "PDF", resolution=300.0 * SCALE)   # физический размер ≈ 14×5 см
    return buf.getvalue(), name + ".pdf"
