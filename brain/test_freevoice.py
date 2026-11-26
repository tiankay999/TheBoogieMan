"""Tests for the free voice stack: how text is cut up, and the live speak-while-thinking path."""

import asyncio
import os

os.environ["BOOGEYMAN_VOICE"] = "0"

import pytest  # noqa: E402

import freevoice as fv  # noqa: E402


# ── cutting a finished reply into pieces ──
def test_short_reply_is_one_piece():
    assert fv.chunks("I dey here, boss.") == ["I dey here, boss."]


def test_opening_piece_is_cut_short():
    """The first piece is the only wait he can't hide, so it must not be the whole paragraph."""
    long_first = "Chale, I dey check the thing for you now, give me small time and I go tell you wetin dey happen."
    out = fv.chunks(long_first + " Second sentence here.")
    assert len(out[0]) <= fv.FIRST_MAX
    assert out[0].endswith(",") or out[0].endswith(".")


def test_later_pieces_are_glued_up():
    out = fv.chunks("One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten.")
    assert len(out) <= 3, f"too many requests: {out}"


def test_nothing_is_lost_when_cutting():
    for text in (
        "I dey here, boss.",
        "Chale, how far? E dey bee. No wahala at all, make I check am for you now.",
        "A single very long clause with no punctuation anywhere in it that just keeps going on",
    ):
        assert "".join(fv.chunks(text)).replace(" ", "") == text.replace(" ", "")


def test_chunks_of_nothing():
    assert fv.chunks("") == []
    assert fv.chunks("   \n  ") == []


# ── cutting a reply that is still arriving ──
def test_opening_clause_is_released_as_soon_as_it_is_complete():
    """A finished sentence goes off to be spoken without waiting for the rest of the answer."""
    p, rest = fv.next_piece("I dey fine, chale. How you dey?", first=True, done=False)
    assert p == "I dey fine, chale."
    assert rest.strip() == "How you dey?"


def test_a_long_opening_is_cut_at_a_comma_rather_than_waited_out():
    buf = "Chale, I dey check the thing for you now, and I go tell you wetin dey happen"
    p, rest = fv.next_piece(buf, first=True, done=False)
    assert p == "Chale, I dey check the thing for you now,"
    assert rest.strip().startswith("and I go tell")


def test_a_stub_is_not_released_on_its_own():
    """"Chale," alone would be a silly thing to say and then pause."""
    p, _ = fv.next_piece("Chale,", first=True, done=False)
    assert p == ""


def test_incomplete_text_waits_for_more():
    p, rest = fv.next_piece("I dey fine and", first=True, done=False)
    assert p == ""
    assert rest == "I dey fine and"


def test_finished_text_is_released_whatever_it_looks_like():
    p, rest = fv.next_piece("Chale", first=True, done=True)
    assert p == "Chale"
    assert rest == ""


def test_a_long_opening_with_no_comma_is_cut_at_a_word():
    buf = "The quick brown fox jumped over the lazy dog and kept running to the river bank"
    p, rest = fv.next_piece(buf, first=True, done=False)
    assert 0 < len(p) <= fv.FIRST_MAX
    assert not p.endswith(" ")
    assert (p + " " + rest).split() == buf.split(), "no words lost"


def test_later_pieces_hold_out_for_a_sentence_end():
    short = "One. Two. Three."
    p, rest = fv.next_piece(short, first=False, done=False)
    assert p == "", "shouldn't bother synthesizing 16 characters while more is coming"
    p, rest = fv.next_piece(short, first=False, done=True)
    assert p == short


def test_a_rambling_piece_is_cut_rather_than_growing_forever():
    buf = "and then, " * 40  # 400 characters, not one full stop
    p, rest = fv.next_piece(buf, first=False, done=False)
    assert p, "a reply with no full stop must still get spoken"
    assert len(p) >= fv.MIN_CHUNK - 2  # the cut lands on MIN_CHUNK, then trailing space is stripped


# ── speaking while the brain is still writing ──
class FakeTTS:
    """Records what it was asked to say, and how much of the text existed at that moment."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.asked: list[str] = []

    async def piece(self, text: str) -> bytes:
        self.asked.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        return b"\x00\x01" * len(text)  # stand-in PCM, length proportional to the text


async def written(parts, gap=0.0):
    for p in parts:
        if gap:
            await asyncio.sleep(gap)
        yield p


def test_speak_live_says_everything_exactly_once():
    tts = FakeTTS()
    parts = ["I dey fine, chale. ", "How you dey? ", "Everything cool."]

    async def go():
        return [c async for c in fv.speak_live(tts.piece, written(parts, gap=0.01))]

    chunks = asyncio.run(go())
    assert chunks, "nothing was spoken"
    assert "".join(tts.asked).replace(" ", "") == "".join(parts).replace(" ", "")


def test_speak_live_starts_before_the_text_is_finished():
    """The whole point: the first request goes out while the brain is still writing."""
    tts = FakeTTS()
    seen_at_first_request: list[int] = []
    sent = []

    async def slow():
        for p in ["I dey fine, chale. ", "How you dey? ", "Still typing. ", "And more. "]:
            await asyncio.sleep(0.02)
            sent.append(p)
            yield p

    async def go():
        async for _ in fv.speak_live(tts.piece, slow()):
            if not seen_at_first_request:
                seen_at_first_request.append(len(sent))

    asyncio.run(go())
    assert seen_at_first_request[0] < 4, (
        f"waited for {seen_at_first_request[0]}/4 of the reply before speaking: no overlap"
    )


def test_speak_live_handles_a_single_short_reply():
    tts = FakeTTS()

    async def go():
        return [c async for c in fv.speak_live(tts.piece, written(["Noted, boss."]))]

    assert asyncio.run(go())
    assert tts.asked == ["Noted, boss."]


def test_speak_live_survives_an_empty_reply():
    tts = FakeTTS()

    async def go():
        return [c async for c in fv.speak_live(tts.piece, written(["", "  "]))]

    assert asyncio.run(go()) == []
    assert tts.asked == []


def test_speak_live_cancels_synthesis_on_barge_in():
    """If he's cut off mid-reply, nothing should be left synthesizing in the background."""
    tts = FakeTTS(delay=0.05)
    parts = ["One sentence here, with a comma. ", "Two. ", "Three. ", "Four. ", "Five. "]

    async def go():
        agen = fv.speak_live(tts.piece, written(parts))
        async for _ in agen:
            break  # barge-in: stop listening immediately
        await agen.aclose()
        await asyncio.sleep(0.15)  # long enough for anything stray to finish
        return len(tts.asked)

    asked = asyncio.run(go())
    assert asked <= fv.LOOKAHEAD, f"{asked} requests kept running after he was interrupted"


# ── decoding ──
def test_decode_wav_resamples_to_the_speaker_rate():
    import io
    import wave

    import numpy as np

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:  # 16 kHz stereo, neither of which the Speaker wants
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.zeros(16000 * 2, dtype=np.int16)).tobytes())
    pcm = fv.decode_wav(buf.getvalue())
    secs = len(pcm) / 2 / fv.TTS_RATE
    assert 0.95 < secs < 1.05, f"a second of audio came back as {secs:.2f}s"


@pytest.mark.parametrize(
    "err,permanent",
    [
        ("quota_exceeded: 0 credits remaining", True),
        ("missing_permissions", True),
        ("HTTP 401 Unauthorized", True),
        ("ReadTimeout", False),
        ("temporarily unavailable", False),
    ],
)
def test_is_permanent(err, permanent):
    assert fv.is_permanent(Exception(err)) is permanent
