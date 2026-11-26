"""Tests for the eyes: the cheap fingerprint, and which monitor gets grabbed.

Nothing here touches a real screen, so these pass on a headless box too. Images are built
from numpy rather than rendered with a font, because the thing under test is how much of the
grid a change moves, and that has to be the same number on every machine.
"""

import io
import os

os.environ["BOOGEYMAN_VOICE"] = "0"

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402

import eyes  # noqa: E402
import study  # noqa: E402

W, H = 1920, 1080
PAPER, INK = 248, 30
GLYPH = (9, 16)  # about what a character covers on a 1080p page


def page(lines: dict[int, list[int]], caret: bool = False) -> Image.Image:
    """A flat page with blocks standing in for text.

    `lines` maps a y position to the x positions of the glyphs on that line.
    """
    a = np.full((H, W, 3), PAPER, dtype=np.uint8)
    for y, xs in lines.items():
        for x in xs:
            a[y : y + GLYPH[1], x : x + GLYPH[0]] = INK
    if caret:
        a[120:142, 1500:1502] = 0
    return Image.fromarray(a)


def line(y: int, n: int, start: int = 120, step: int = 11) -> dict[int, list[int]]:
    return {y: [start + i * step for i in range(n)]}


# A page of four option rows that never change, and one question line that does. That is the
# shape of every quiz app, and the reason the first threshold guess was wrong.
BODY = {255 + i * 62: [145 + j * 11 for j in range(14)] for i in range(4)}
A = page({**BODY, **line(165, 46)})
# A new question, not the same one nudged sideways. Shifting a line of equal length is almost
# invisible to the grid (0.00018, below MOVED) because each block still averages the same
# amount of ink; what a real question swap changes is how much text there is. Calibrated
# against rendered quiz pages, where the real swap measured 0.00186.
B = page({**BODY, **line(165, 28)})
A_CARET = page({**BODY, **line(165, 46)}, caret=True)


def test_threshold_sits_between_the_measurements():
    """The bug this catches: MOVED set high enough that a new question is never noticed.

    Measured over rendered quiz screens, a question swapped inside an identical layout moves
    the grid about 0.0016, while a blinking caret moves it 0.00003. A threshold outside that
    band either goes deaf to the next question or fires on nothing at all.
    """
    assert 0.0001 < study.MOVED < 0.0016, (
        f"MOVED={study.MOVED} is outside the measured band; see eyes.drift()"
    )


def test_a_changed_question_fires():
    assert eyes.drift(eyes._grey(A), eyes._grey(B)) > study.MOVED


def test_a_blinking_caret_does_not_fire():
    assert eyes.drift(eyes._grey(A), eyes._grey(A_CARET)) < study.MOVED


def test_a_changed_question_outweighs_a_caret():
    """Not just both sides of a line: the gap has to be wide enough to set a threshold in."""
    signal = eyes.drift(eyes._grey(A), eyes._grey(B))
    noise = eyes.drift(eyes._grey(A), eyes._grey(A_CARET))
    assert signal > noise * 10, f"signal {signal:.5f} vs noise {noise:.5f}"


def test_the_same_screen_does_not_drift():
    assert eyes.drift(eyes._grey(A), eyes._grey(A)) == 0.0


def test_nothing_to_compare_counts_as_changed():
    """A missing baseline must read as "everything changed", never as "nothing happened"."""
    assert eyes.drift(None, eyes._grey(A)) == 1.0
    assert eyes.drift(eyes._grey(A), None) == 1.0
    assert eyes.drift(None, None) == 1.0


def test_fingerprint_reads_encoded_bytes():
    buf = io.BytesIO()
    A.save(buf, format="JPEG", quality=eyes.QUALITY)
    fp = eyes.fingerprint(buf.getvalue())
    assert fp.shape == (eyes.GRID, eyes.GRID)
    assert 0.0 <= float(fp.min()) and float(fp.max()) <= 1.0


def test_fingerprint_survives_the_encoder():
    """JPEG noise must not swamp the threshold, or every look would trigger another."""
    buf = io.BytesIO()
    A.save(buf, format="JPEG", quality=eyes.QUALITY)
    assert eyes.drift(eyes._grey(A), eyes.fingerprint(buf.getvalue())) < study.MOVED


def test_a_small_image_still_fingerprints():
    """reduce() is skipped below a size; the grid must still come out the right shape."""
    tiny = Image.fromarray(np.full((40, 60, 3), PAPER, dtype=np.uint8))
    assert eyes._grey(tiny).shape == (eyes.GRID, eyes.GRID)


# ── which monitor ──
def test_one_monitor_uses_it():
    assert eyes._pick([{"n": "union"}], 0) == {"n": "union"}


def test_default_is_the_primary_not_the_union():
    """mss index 0 is every screen glued together; grabbing it letterboxes the quiz."""
    mons = [{"n": "union"}, {"n": "primary"}, {"n": "second"}]
    assert eyes._pick(mons, 0) == {"n": "primary"}


def test_an_explicit_monitor_is_honoured():
    mons = [{"n": "union"}, {"n": "primary"}, {"n": "second"}]
    assert eyes._pick(mons, 2) == {"n": "second"}


def test_a_monitor_that_isnt_there_falls_back():
    mons = [{"n": "union"}, {"n": "primary"}]
    assert eyes._pick(mons, 7) == {"n": "primary"}


def test_no_libraries_means_blind(monkeypatch):
    monkeypatch.setattr(eyes, "HAVE_EYES", False)
    with pytest.raises(eyes.Blind):
        eyes.monitors()
    with pytest.raises(eyes.Blind):
        eyes._capture(0)
