"""
status_image.py — генерация красивых status-картинок для VaultHost.

Использует Pillow для рисования тёмных карточек с градиентами,
прогресс-барами и типографикой.  Для кириллицы нужен шрифт с
поддержкой Unicode — по умолчанию ищет в системе; если не найден,
генерирует ASCII-заглушку.
"""

import io
import math
import os
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ─── palette ───────────────────────────────────────────────────────────────────

BG_DARK     = (15, 15, 25)
BG_CARD     = (22, 27, 44)
BORDER      = (40, 48, 75)
ACCENT      = (90, 70, 220)          # фиолетовый акцент VaultHost
ACCENT_L    = (120, 100, 255)
GREEN       = (52, 211, 153)
YELLOW      = (251, 191, 36)
RED         = (248, 113, 113)
WHITE       = (235, 235, 245)
GRAY        = (140, 150, 180)
DIM         = (80, 85, 110)
BAR_BG      = (35, 40, 60)

# ─── fonts ─────────────────────────────────────────────────────────────────────

_SEARCH_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]

_SEARCH_PATHS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    "C:\\Windows\\Fonts\\segoeuib.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]

_SEARCH_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/local/share/fonts/DejaVuSans.ttf",
    "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\segoeui.ttf",
    "C:\\Windows\\Fonts\\arial.ttf",
]

_font_cache: dict = {}


def _find_font(bold: bool = False) -> str:
    key = "bold" if bold else "regular"
    if key in _font_cache:
        return _font_cache[key]

    # На Docker-образах шрифт может лежать рядом с проектом
    local = Path(__file__).parent / "fonts" / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    if local.exists():
        _font_cache[key] = str(local)
        return _font_cache[key]

    for p in (_SEARCH_PATHS_BOLD if bold else _SEARCH_PATHS_REGULAR):
        if os.path.isfile(p):
            _font_cache[key] = p
            return _font_cache[key]

    # Фоллбэк — Pillow подставит дефолтный шрифт
    _font_cache[key] = ""
    return ""


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _find_font(bold)
    try:
        if path:
            return ImageFont.truetype(path, size)
    except Exception:
        pass
    return ImageFont.load_default()


# ─── drawing helpers ──────────────────────────────────────────────────────────

def _rounded_rect(draw: ImageDraw.ImageDraw, bbox, radius: int, fill=None, outline=None, width=1):
    x0, y0, x1, y1 = bbox
    if fill:
        draw.rectangle([x0 + radius, y0, x1 - radius, y1], fill=fill)
        draw.rectangle([x0, y0 + radius, x1, y1 - radius], fill=fill)
        draw.pieslice([x0, y0, x0 + 2 * radius, y0 + 2 * radius], 180, 270, fill=fill)
        draw.pieslice([x1 - 2 * radius, y0, x1, y0 + 2 * radius], 270, 360, fill=fill)
        draw.pieslice([x0, y1 - 2 * radius, x0 + 2 * radius, y1], 90, 180, fill=fill)
        draw.pieslice([x1 - 2 * radius, y1 - 2 * radius, x1, y1], 0, 90, fill=fill)
    if outline:
        draw.arc([x0, y0, x0 + 2 * radius, y0 + 2 * radius], 180, 270, fill=outline, width=width)
        draw.arc([x1 - 2 * radius, y0, x1, y0 + 2 * radius], 270, 360, fill=outline, width=width)
        draw.arc([x0, y1 - 2 * radius, x0 + 2 * radius, y1], 90, 180, fill=outline, width=width)
        draw.arc([x1 - 2 * radius, y1 - 2 * radius, x1, y1], 0, 90, fill=outline, width=width)
        draw.line([x0 + radius, y0, x1 - radius, y0], fill=outline, width=width)
        draw.line([x0 + radius, y1, x1 - radius, y1], fill=outline, width=width)
        draw.line([x0, y0 + radius, x0, y1 - radius], fill=outline, width=width)
        draw.line([x1, y0 + radius, x1, y1 - radius], fill=outline, width=width)


def _bar_color(pct: float, warn: float = 60, crit: float = 85):
    if pct >= crit:
        return RED
    if pct >= warn:
        return YELLOW
    return GREEN


def _draw_bar(draw: ImageDraw.ImageDraw, x, y, w, h, pct: float, label: str = ""):
    r = h // 2
    _rounded_rect(draw, (x, y, x + w, y + h), r, fill=BAR_BG)
    fill_w = max(r * 2, int(w * min(pct, 100) / 100))
    color = _bar_color(pct)
    _rounded_rect(draw, (x, y, x + fill_w, y + h), r, fill=color)
    if label:
        draw.text((x + w + 12, y - 1), label, fill=WHITE, font=_font(13, bold=True))


def _draw_gradient_top(img: Image.Image, height: int = 120):
    """Фиолетовый градиент сверху."""
    draw = ImageDraw.Draw(img)
    w = img.width
    for row in range(height):
        t = row / height
        r = int(ACCENT[0] * (1 - t * 0.85))
        g = int(ACCENT[1] * (1 - t * 0.85))
        b = int(ACCENT[2] * (1 - t * 0.85))
        draw.line([(0, row), (w, row)], fill=(r, g, b))


def _text_centered(draw: ImageDraw.ImageDraw, y, text, font, fill, width: int = None):
    """Рисует текст по центру. width — ширина изображения (обязательно для Pillow 10+)."""
    if width is None:
        try:
            width = draw.im.size[0]
        except Exception:
            width = 520
    bbox = draw.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    draw.text(((width - w) // 2, y), text, fill=fill, font=font)


def _text(draw: ImageDraw.ImageDraw, x, y, text, font, fill):
    draw.text((x, y), text, fill=fill, font=font)


# ─── server stats card ─────────────────────────────────────────────────────────

def generate_server_stats(
    server_name: str,
    cpu_pct: float,
    mem_mb: float,
    mem_limit: float = 50,
    cpu_limit: float = 25,
    uptime: str = "—",
    container_id: str = "",
) -> bytes:
    W, H = 520, 380
    img = Image.new("RGB", (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)
    _draw_gradient_top(img, 100)

    # Заголовок
    _text_centered(draw, 18, "SERVER STATISTICS", _font(22, bold=True), WHITE, W)
    _text_centered(draw, 48, f"{server_name}", _font(15), ACCENT_L, W)

    # Разделительная полоса
    draw.rectangle([30, 78, W - 30, 79], fill=BORDER)

    y = 95
    pad = 40

    # CPU
    _text(draw, pad, y, "CPU", _font(14, bold=True), GRAY)
    cpu_usage = min(cpu_pct, cpu_limit)
    cpu_bar_pct = (cpu_usage / cpu_limit) * 100 if cpu_limit else 0
    _draw_bar(draw, pad, y + 24, W - pad - 190, 14, cpu_bar_pct,
              f"{cpu_pct:.1f}% / {cpu_limit:.0f}%")
    y += 65

    # RAM
    _text(draw, pad, y, "RAM", _font(14, bold=True), GRAY)
    mem_pct = (mem_mb / mem_limit) * 100 if mem_limit else 0
    _draw_bar(draw, pad, y + 24, W - pad - 190, 14, mem_pct,
              f"{mem_mb:.0f} / {mem_limit:.0f} MB")
    y += 65

    # Info
    draw.rectangle([pad, y, W - pad, y + 1], fill=BORDER)
    y += 12
    info_font = _font(12)
    dim_font  = _font(12)
    _text(draw, pad, y,      f"UPTIME",  dim_font, DIM)
    _text(draw, pad + 80, y, uptime,     info_font, GRAY)
    y += 24
    _text(draw, pad, y,      f"DISK",    dim_font, DIM)
    _text(draw, pad + 80, y, "/app (Docker Volume)", info_font, GRAY)
    y += 24
    _text(draw, pad, y,      f"NETWORK", dim_font, DIM)
    _text(draw, pad + 80, y, "Isolated bridge + internet", info_font, GRAY)
    y += 24
    _text(draw, pad, y,      f"LIMITS",  dim_font, DIM)
    _text(draw, pad + 80, y, f"{int(mem_limit)} MB RAM · {cpu_limit/100:.2f} vCPU", info_font, GRAY)

    # Watermark
    _text_centered(draw, H - 28, "VaultHost", _font(11), DIM, W)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()


# ─── service status card ──────────────────────────────────────────────────────

def generate_service_status(
    platform_ok: bool,
    total_servers: int,
    running: int,
    stopped: int,
    uptime_label: str = "Normal",
) -> bytes:
    W, H = 520, 420
    img = Image.new("RGB", (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)
    _draw_gradient_top(img, 100)

    # Заголовок
    _text_centered(draw, 18, "VAULTHOST STATUS", _font(22, bold=True), WHITE, W)
    _text_centered(draw, 48, "Platform Overview", _font(15), ACCENT_L, W)

    draw.rectangle([30, 78, W - 30, 79], fill=BORDER)

    pad = 40
    y = 95

    # Platform status
    icon_color = GREEN if platform_ok else RED
    status_text = "Operational" if platform_ok else "Docker Unavailable"
    _text(draw, pad, y, "PLATFORM", _font(14, bold=True), GRAY)
    _text(draw, pad + 110, y, "● " + status_text, _font(14, bold=True), icon_color)
    y += 40

    # Статистика в карточках
    card_h = 72
    card_w = (W - pad * 2 - 20) // 2
    gap = 20

    # Total servers
    cx = pad
    _rounded_rect(draw, (cx, y, cx + card_w, y + card_h), 12, fill=BG_CARD, outline=BORDER)
    _text(draw, cx + 16, y + 12, "TOTAL SERVERS", _font(11), DIM)
    _text(draw, cx + 16, y + 34, str(total_servers), _font(30, bold=True), WHITE)

    # Running
    cx = pad + card_w + gap
    _rounded_rect(draw, (cx, y, cx + card_w, y + card_h), 12, fill=BG_CARD, outline=BORDER)
    _text(draw, cx + 16, y + 12, "RUNNING", _font(11), DIM)
    _text(draw, cx + 16, y + 34, str(running), _font(30, bold=True), GREEN)

    y += card_h + 16

    # Stopped
    cx = pad
    _rounded_rect(draw, (cx, y, cx + card_w, y + card_h), 12, fill=BG_CARD, outline=BORDER)
    _text(draw, cx + 16, y + 12, "STOPPED", _font(11), DIM)
    _text(draw, cx + 16, y + 34, str(stopped), _font(30, bold=True), RED)

    # Utilization
    cx = pad + card_w + gap
    _rounded_rect(draw, (cx, y, cx + card_w, y + card_h), 12, fill=BG_CARD, outline=BORDER)
    util_pct = (running / total_servers * 100) if total_servers else 0
    _text(draw, cx + 16, y + 12, "UTILIZATION", _font(11), DIM)
    _text(draw, cx + 16, y + 34, f"{util_pct:.0f}%", _font(30, bold=True), ACCENT_L)

    y += card_h + 20

    # Прогресс-бар утилизации
    _text(draw, pad, y, "UTILIZATION", _font(12), DIM)
    _draw_bar(draw, pad + 110, y + 2, W - pad - 110 - 70, 12, util_pct, f"{util_pct:.1f}%")
    y += 30

    # Version
    draw.rectangle([pad, y, W - pad, y + 1], fill=BORDER)
    y += 10
    _text(draw, pad, y + 4, "VERSION", _font(12), DIM)
    _text(draw, pad + 110, y + 4, "VaultHost v1.0", _font(12, bold=True), GRAY)

    # Watermark
    _text_centered(draw, H - 28, "VaultHost", _font(11), DIM, W)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()
