"""Tests for study mode: what he reads, what he says, and what he keeps quiet about.

The screen and the model are both stubbed. What's under test is the loop's judgement: when a
look is worth paying for, when an answer is worth saying out loud, and the several ways it
could end up talking to itself.
"""

import asyncio
import os

os.environ["BOOGEYMAN_VOICE"] = "0"

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import eyes  # noqa: E402
import study as st  # noqa: E402


# ── reading the model's reply ──
def test_parse_a_clean_reply():
    got = st.parse("QUESTION: Capital of Ghana\nANSWER: B. Accra\nWHY: It is the seat of government.")
    assert got["QUESTION"] == "Capital of Ghana"
    assert got["ANSWER"] == "B. Accra"
    assert got["WHY"] == "It is the seat of government."


def test_parse_survives_markdown():
    """Models bold the labels when they feel like it; that must not lose the answer."""
    got = st.parse("**QUESTION:** Capital of Ghana\n**ANSWER:** *B. Accra*\n- WHY: `It is the capital.`")
    assert got["QUESTION"] == "Capital of Ghana"
    assert got["ANSWER"] == "B. Accra"
    assert got["WHY"] == "It is the capital."


def test_parse_a_missing_line():
    got = st.parse("QUESTION: Something\nANSWER: 42")
    assert got["ANSWER"] == "42"
    assert "WHY" not in got


def test_parse_nothing():
    assert st.parse("") == {}
    assert st.parse("I'm not sure what you mean.") == {}


def test_parse_keeps_the_first_of_a_repeated_field():
    got = st.parse("QUESTION: First\nANSWER: One\nQUESTION: Second\nANSWER: Two")
    assert got["QUESTION"] == "First" and got["ANSWER"] == "One"


@pytest.mark.parametrize("text", ["NONE", "none", "N/A", "nothing", "No question"])
def test_none_is_recognised(text):
    assert st.NONE.match(text)


@pytest.mark.parametrize("text", ["Nonetheless this is a question", "No, which one?"])
def test_none_is_not_over_eager(text):
    assert not st.NONE.match(text)


def test_key_ignores_rewording_noise():
    assert st.key_of("Capital of Ghana?") == st.key_of("  capital of ghana  ")
    assert st.key_of("Q7: Capital of Ghana!") != st.key_of("Capital of Nigeria")


# ── the spoken triggers ──
@pytest.mark.parametrize("text", [
    "study mode", "quiz mode", "studying mode", "watch my screen", "watch the screen",
    "let's study", "start studying", "start study", "study with me", "help me revise",
])
def test_turning_it_on(text):
    assert st.wanted(text) == "on"


@pytest.mark.parametrize("text", ["read my screen", "look at my screen", "what's on my screen"])
def test_a_one_off_look_does_not_start_watching(text):
    """These want one answer, not a background watcher.

    They fall through to the LLM, which has look_at_screen for exactly this. Starting a mode
    when he asked a single question is the more annoying way to be wrong.
    """
    assert st.wanted(text) is None


@pytest.mark.parametrize("text", [
    "exit study mode", "stop studying", "study mode off", "quiz mode off",
    "stop watching my screen", "turn off study mode", "stop watching",
])
def test_turning_it_off(text):
    assert st.wanted(text) == "off"


@pytest.mark.parametrize("text", ["further more", "furthermore", "go deeper", "explain that", "break it down"])
def test_asking_for_more(text):
    assert st.wanted(text) == "more"


@pytest.mark.parametrize("text", [
    "how far boss",
    "open notepad",
    "what's the weather",
    # Long sentences are talk, not commands - the same rule the other built-ins use.
    "I was studying all night and I still could not explain that topic to anybody at all",
])
def test_ordinary_talk_is_not_a_command(text):
    assert st.wanted(text) is None


def test_off_wins_over_on():
    """"exit study mode" contains "study mode"; the wrong order would toggle it back on."""
    assert st.wanted("exit study mode") == "off"


# ── the loop ──
class Fake:
    """A stubbed screen and model. `screen` is swapped to simulate the page changing."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.screen = 0
        self.looks = 0
        self.said: list[str] = []
        self.events: list[tuple] = []

    async def grab(self, which=None):
        return b"shot-%d" % self.screen

    async def sense(self, which=None):
        # Each screen is a flat grid at its own brightness, so switching screens drifts hard
        # and staying put drifts not at all.
        return np.full((eyes.GRID, eyes.GRID), 0.1 * self.screen, dtype=np.float32)

    async def look(self, system, prompt, img, media="image/jpeg"):
        self.looks += 1
        return self.replies[min(self.looks - 1, len(self.replies) - 1)]

    async def say(self, text):
        self.said.append(text)

    async def emit(self, kind, payload):
        self.events.append((kind, payload))


def run(fake, monkeypatch, steps=14, quiet=None, between=None):
    """Drive the watcher for a bounded number of polls, with no real sleeping."""
    monkeypatch.setattr(eyes, "grab", fake.grab)
    monkeypatch.setattr(eyes, "sense", fake.sense)
    monkeypatch.setattr(st, "POLL", 0)
    monkeypatch.setattr(st, "SETTLE", 1)
    monkeypatch.setattr(st, "COOLDOWN", 0.0)

    async def go():
        s = st.Study(fake.look, fake.say, fake.emit, quiet=quiet)
        assert await s.start() == "Watching your screen, boss."
        for i in range(steps):
            await asyncio.sleep(0)
            if between:
                between(i, fake)
        # let the loop drain
        for _ in range(40):
            await asyncio.sleep(0)
        await s.stop()
        return s

    return asyncio.run(go())


ONE = "QUESTION: Capital of Ghana\nANSWER: B. Accra\nWHY: It is the seat of government."
TWO = "QUESTION: Largest planet\nANSWER: C. Jupiter\nWHY: It is the biggest in the solar system."
NOTHING = "QUESTION: NONE"


def test_he_answers_what_is_on_screen(monkeypatch):
    fake = Fake([ONE])
    run(fake, monkeypatch)
    assert fake.said, "he said nothing at all"
    assert fake.said[0].startswith("B. Accra"), fake.said


def test_the_answer_comes_before_the_reason(monkeypatch):
    """The whole point of the mode: the verdict first, the why as a bonus."""
    fake = Fake([ONE])
    run(fake, monkeypatch)
    said = fake.said[0]
    assert said.index("Accra") < said.index("seat of government")


def test_he_does_not_repeat_the_same_question(monkeypatch):
    fake = Fake([ONE, ONE, ONE, ONE])
    run(fake, monkeypatch, steps=30)
    assert len(fake.said) == 1, fake.said


def test_a_new_question_gets_a_new_answer(monkeypatch):
    fake = Fake([ONE, TWO])

    def flip(i, f):
        if i == 6:
            f.screen = 1  # the page moves on

    run(fake, monkeypatch, steps=30, between=flip)
    assert len(fake.said) == 2, fake.said
    assert "Jupiter" in fake.said[1]


def test_nothing_on_screen_means_silence(monkeypatch):
    """Not conversational: with no question in front of him he says nothing."""
    fake = Fake([NOTHING])
    run(fake, monkeypatch)
    assert fake.said == []
    assert fake.events == []


def test_an_answerless_reply_is_not_spoken(monkeypatch):
    fake = Fake(["QUESTION: Something\nWHY: no answer came back"])
    run(fake, monkeypatch)
    assert fake.said == []


def test_he_does_not_look_while_he_is_talking(monkeypatch):
    """His captions are on the screen he is watching, so looking then reads his own words."""
    fake = Fake([ONE])
    run(fake, monkeypatch, steps=20, quiet=lambda: False)
    assert fake.looks == 0, "looked at the screen while speaking"
    assert fake.said == []


def test_he_sweeps_a_still_screen_eventually(monkeypatch):
    """Drift can miss a question swapped for one of equal text density, so he looks anyway.

    The dedup is what makes this affordable: sweeping costs looks, never repeated answers.
    """
    fake = Fake([ONE])
    monkeypatch.setattr(st, "SWEEP", 0.0)
    run(fake, monkeypatch, steps=30)
    assert fake.looks > 1, "never looked again at a screen that stopped changing"
    assert len(fake.said) == 1, f"the sweep made him repeat himself: {fake.said}"


def test_a_sweep_still_reports_a_quietly_changed_question(monkeypatch):
    """The point of the sweep: the page moved on without moving enough pixels to notice."""
    fake = Fake([ONE, TWO])
    monkeypatch.setattr(st, "SWEEP", 0.0)
    run(fake, monkeypatch, steps=30)  # fake.screen never changes, so drift never fires
    assert len(fake.said) == 2, fake.said
    assert "Jupiter" in fake.said[1]


def test_an_answer_is_announced_to_the_ui(monkeypatch):
    fake = Fake([ONE])
    run(fake, monkeypatch)
    kinds = [k for k, _ in fake.events]
    assert kinds == ["study.answer"]
    payload = fake.events[0][1]
    assert payload["answer"] == "B. Accra"
    assert payload["question"] == "Capital of Ghana"


def test_a_failing_look_does_not_kill_the_watcher(monkeypatch):
    fake = Fake([ONE])
    calls = {"n": 0}

    async def flaky(system, prompt, img, media="image/jpeg"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return ONE

    fake.look = flaky
    monkeypatch.setattr(st, "BACKOFF", 0)
    run(fake, monkeypatch, steps=40)
    assert calls["n"] >= 2, "gave up after the first failure"


# ── further more ──
def test_further_with_nothing_to_go_on():
    fake = Fake([ONE])
    s = st.Study(fake.look, fake.say, fake.emit)
    assert "Nothing to go further on" in asyncio.run(s.further())


def test_further_reuses_the_screenshot_he_read():
    """No second grab: the page may already have moved on."""
    fake = Fake(["The long version."])
    s = st.Study(fake.look, fake.say, fake.emit)
    s.last_shot, s.last_answer = b"shot-0", "B. Accra"
    seen = {}

    async def look(system, prompt, img, media="image/jpeg"):
        seen["img"] = img
        seen["prompt"] = prompt
        return "The long version."

    s.look = look
    assert asyncio.run(s.further()) == "The long version."
    assert seen["img"] == b"shot-0"
    assert "B. Accra" in seen["prompt"]


def test_further_survives_a_broken_call():
    fake = Fake([ONE])
    s = st.Study(fake.look, fake.say, fake.emit)
    s.last_shot = b"shot-0"

    async def boom(*a, **k):
        raise RuntimeError("no key")

    s.look = boom
    assert "Couldn't get into it" in asyncio.run(s.further())


# ── turning it on and off ──
def test_starting_without_eyes(monkeypatch):
    monkeypatch.setattr(eyes, "HAVE_EYES", False)
    fake = Fake([ONE])
    s = st.Study(fake.look, fake.say, fake.emit)
    out = asyncio.run(s.start())
    assert "can't see your screen" in out
    assert not s.on


def test_starting_when_the_screen_cannot_be_read(monkeypatch):
    async def blind(which=None):
        raise eyes.Blind("session is locked")

    monkeypatch.setattr(eyes, "sense", blind)
    fake = Fake([ONE])
    s = st.Study(fake.look, fake.say, fake.emit)
    assert "can't get at your screen" in asyncio.run(s.start())
    assert not s.on


def test_starting_twice_is_harmless(monkeypatch):
    monkeypatch.setattr(eyes, "sense", Fake([ONE]).sense)
    fake = Fake([ONE])

    async def go():
        s = st.Study(fake.look, fake.say, fake.emit)
        await s.start()
        second = await s.start()
        await s.stop()
        return second

    assert asyncio.run(go()) == "Already watching, boss."


def test_stopping_when_never_started():
    fake = Fake([ONE])
    s = st.Study(fake.look, fake.say, fake.emit)
    assert asyncio.run(s.stop()) == "Stopped watching, boss."
