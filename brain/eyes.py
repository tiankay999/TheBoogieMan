"""The Boogeyman's eyes: what is actually on the screen right now.

Two paths, and keeping them apart is the whole point.

A *look* is the expensive one: grab, downscale, encode, upload, and a second or so of model
time. A *sense* is the cheap one: the screen crushed to a 32x32 grey grid, which costs about
90 ms and no money at all. Comparing two senses says whether anything happened. Study mode
leans on that - it senses twice a second and only spends a look once the picture has moved
and then gone still - so the cheap path skips the full-size resize and the encoder entirely.

Measured on a 1920x1080 screen, which is why the defaults are what they are:

    capture (mss)                  58 ms
    bytes -> RGB                   24 ms
    reduce(8) -> 32x32 grey         4 ms   <- sense stops here, ~86 ms
    resize LANCZOS to 1568        111 ms
    JPEG q92 encode                14 ms   262 KB   <- look, ~210 ms
    PNG level 1 encode            107 ms   862 KB   (the alternative, 3.3x the bytes)

JPEG rather than PNG because the usual objection to it does not survive measurement: asked to
transcribe 11-pixel footer text and a random serial number off a rendered quiz, the model read
both exactly from JPEG q85 and up, at 1568 and at full width. Lossless was buying nothing but
upload time.

Grabs are deliberately stateless. mss keeps per-thread device contexts on Windows and is not
safe to share, and these calls run on asyncio's thread pool where the thread is whichever one
is free, so each grab opens and closes its own. That costs a few milliseconds and saves a
class of bug that would only ever show up under load.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Any

import numpy as np

log = logging.getLogger("brain.eyes")

try:
    import mss
    from PIL import Image

    HAVE_EYES = True
except ImportError:  # pragma: no cover - exercised by the import guard in main.py
    HAVE_EYES = False

# Anthropic resizes anything longer than this server-side anyway, so sending more is paying
# upload time for pixels that get thrown away.
MAX_EDGE = int(os.environ.get("BOOGEYMAN_SCREEN_EDGE", "1568"))
QUALITY = int(os.environ.get("BOOGEYMAN_SCREEN_QUALITY", "92"))
MEDIA = "image/jpeg"
GRID = 48  # sense resolution; see drift() for why this is enough and why bigger buys nothing
PRESHRINK = 8  # integer box-reduce before the grey downsample; 28x faster than resizing full-size
SCREEN = int(os.environ.get("BOOGEYMAN_SCREEN", "0"))  # 0 = primary


class Blind(Exception):
    """No screen capture available (mss or Pillow missing, or the grab failed)."""


def monitors() -> list[dict[str, Any]]:
    """Every monitor, as mss sees it. Index 0 is the whole virtual desktop."""
    if not HAVE_EYES:
        raise Blind("screen capture needs mss and pillow: pip install mss pillow")
    with mss.MSS() as sct:
        return [dict(m) for m in sct.monitors]


def _pick(mons: list[Any], which: int) -> Any:
    """Which monitor to grab.

    mss puts the union of all screens at index 0 and the real ones from 1. Grabbing the union
    on a multi-monitor setup hands the model a letterboxed double-width image with the quiz in
    one half, so "0" here means the primary screen, not the union.
    """
    if which <= 0:
        return mons[1] if len(mons) > 1 else mons[0]
    if which < len(mons):
        return mons[which]
    log.warning("no monitor %d, using the primary one", which)
    return mons[1] if len(mons) > 1 else mons[0]


def _capture(which: int) -> Any:
    if not HAVE_EYES:
        raise Blind("screen capture needs mss and pillow: pip install mss pillow")
    try:
        with mss.MSS() as sct:
            raw = sct.grab(_pick(sct.monitors, which))
            # BGRX, not BGRA: the fourth byte of a desktop grab is padding, and calling it alpha
            # makes Pillow composite against nothing and wash the colours out.
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Blind:
        raise
    except Exception as e:  # mss has its own error types, and a locked session raises outright
        raise Blind(f"couldn't read the screen: {e}") from e


def _shot(which: int) -> bytes:
    img = _capture(which)
    w, h = img.size
    scale = MAX_EDGE / max(w, h)
    if scale < 1:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=QUALITY)
    return buf.getvalue()


def _grey(img: Any) -> np.ndarray:
    """An image down to a GRID x GRID grey map, 0..1.

    reduce() first because it is an integer box average and runs on bytes; going straight to
    GRID with a resize costs 111 ms against 4 ms for the same answer. BOX averages whole blocks
    rather than sampling points, so a blinking caret or a drifting mouse barely registers while
    a changed question moves a whole band of the grid.
    """
    small = img.reduce(PRESHRINK) if min(img.size) >= PRESHRINK * GRID else img
    return np.asarray(small.convert("L").resize((GRID, GRID), Image.BOX), dtype=np.float32) / 255.0


def _sense(which: int) -> np.ndarray:
    return _grey(_capture(which))


async def grab(which: int | None = None) -> bytes:
    """One screenshot as JPEG bytes, downscaled to MAX_EDGE. The expensive path."""
    return await asyncio.to_thread(_shot, SCREEN if which is None else which)


async def sense(which: int | None = None) -> np.ndarray:
    """A fingerprint of the screen, without encoding an image. The cheap path."""
    return await asyncio.to_thread(_sense, SCREEN if which is None else which)


def fingerprint(data: bytes) -> np.ndarray:
    """The same fingerprint, from encoded image bytes. Used by tests and by look callers."""
    return _grey(Image.open(io.BytesIO(data)))


def drift(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """How much two fingerprints differ: 0 identical, 1 black against white.

    The scale is much finer than it looks, and the case that sets it is the quiet one. A quiz
    app redrawing the same layout with a new question in it - the thing this whole feature
    exists to notice - moves the grid by only about 0.0016, because text is thin and averaging
    a block of it mostly returns background either way. Measured over rendered quiz screens:

        same layout, new question   0.00186   <- must fire
        an option clicked           0.00110
        a clock digit changing      0.00005
        a caret blinking            0.00003   <- must not fire
        content scrolled            0.1102
        a still screen              0.0000

    So the useful threshold sits near 0.0005: eight times the worst noise, and a third of the
    weakest real signal. GRID 48 was picked off that table - it separates those two best (38x
    against 28x at GRID 32) and costs the same 2 ms, while going to 128 buys nothing more.
    """
    if a is None or b is None:
        return 1.0
    return float(np.abs(a - b).mean())
