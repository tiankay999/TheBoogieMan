"""Study mode: he watches the screen and calls the answer as each question appears.

Not a conversation. While this is on he says nothing at all unless there is a question in
front of him, and when there is he gives the answer and one short line of why - enough to
learn from without turning a drill session into a lecture. "Further more" opens up the last
one properly.

How it avoids burning money and talking over itself:

  * Looking costs a model call; noticing costs 63 ms. The loop only ever senses (see eyes.py),
    and spends a look when the screen has moved and then gone still for SETTLE polls. A page
    being drawn, a list animating in, a scroll in progress - none of those get read half-done.
  * Every look is deduplicated on the question itself, so clicking an option, moving the mouse
    or a timer ticking down re-reads nothing. A repeat answer is never spoken twice.
  * His own captions are on the same screen he is watching. Answering therefore changes the
    screen, which would trigger a look at his own words - a loop that costs a call every time
    he opens his mouth. So the watcher holds still while he is speaking and re-baselines
    afterwards, treating whatever his talking changed as the new normal.
  * Change detection uses eyes.sense() for every reading, never a mix of sense() and
    fingerprint(grab()). The two downscale by different routes and disagree by about 0.014,
    which is larger than MOVED: mixing them invents changes that never happened.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import eyes

log = logging.getLogger("brain.study")

POLL = float(os.environ.get("BOOGEYMAN_STUDY_POLL", "0.3"))  # seconds between senses
# Deliberately tiny: a quiz app swapping the question inside an identical layout only moves
# the fingerprint by about 0.0016, while a blinking caret moves it 0.00003. See eyes.drift()
# for the measurements this sits between. Set it too high - 0.010 was the first guess - and he
# silently never notices the next question, which is the one failure that looks like success.
MOVED = float(os.environ.get("BOOGEYMAN_STUDY_MOVED", "0.0005"))
SETTLE = int(os.environ.get("BOOGEYMAN_STUDY_SETTLE", "2"))  # still senses before he reads
COOLDOWN = float(os.environ.get("BOOGEYMAN_STUDY_COOLDOWN", "1.2"))  # floor between looks
# The safety net. Drift notices a changed question because the new words carry a different
# amount of ink, but two questions can happen to carry the same: measured on rendered pages,
# a line of text replaced by one of equal length drifts 0.00018 and a single changed option
# 0.00033, both under MOVED. Those would be missed in silence, which is the one failure that
# looks exactly like working. So he looks anyway this often, and the dedup keeps him quiet
# unless the question really did move on.
SWEEP = float(os.environ.get("BOOGEYMAN_STUDY_SWEEP", "30"))
BACKOFF = 5.0  # pause after a failed look, so a dead key doesn't spin the loop
RETRIES = 3  # attempts at the same screen before he waits for it to change on its own

SYSTEM = (
    "You read a screen for someone who is studying. You are not having a conversation and you "
    "are not an assistant here: you are an answer key.\n"
    "Reply in exactly this shape, three lines, nothing before or after:\n"
    "QUESTION: <the question in at most 12 words, or NONE>\n"
    "ANSWER: <the answer itself, as short as it can be said>\n"
    "WHY: <at most 10 words, and fewer is better>\n\n"
    "Rules:\n"
    "- If nothing on the screen is asking a question, reply with exactly: QUESTION: NONE\n"
    "- Multiple choice: ANSWER is the option letter, then the option's own words.\n"
    "- Never greet, never describe the screen, never mention looking, never offer help.\n"
    "- Unsure: still commit to your best answer, and start WHY with 'Not certain:'.\n"
    "- WHY jogs the memory, it does not teach. \"It makes most of the cell's ATP\" - that "
    "short. He is drilling, and every extra word is another second before the next question. "
    "If he wants the lesson, he asks for it."
)
PROMPT = "What is the question on this screen, and what is the answer?"
DEEPER = (
    "Explain this question properly now: why the right answer is right, and why each other "
    "option is wrong. Six sentences at most, spoken plainly, no lists and no headings."
)

FIELD = re.compile(r"^[^A-Za-z]*(QUESTION|ANSWER|WHY)\s*:\s*(.*)$", re.I | re.M)
NONE = re.compile(r"^(none|n/?a|nothing|no question)\b", re.I)

# Spoken triggers. They live here rather than in commands.py because turning this on can fail
# (no screen, no vision model) and the reply has to come from start(), which is async.
OFF = re.compile(
    r"\b((exit|leave|quit|stop|end|disable|turn\s*off|cancel|close|kill|drop)\s*(the\s*)?"
    r"(study|quiz|revision)(\s*mode)?|(study|quiz|revision)\s*mode\s*off|"
    r"stop\s*(watching|reading)(\s*(my|the))?(\s*screen)?|stop\s*studying)\b"
)
ON = re.compile(
    r"\b((study|quiz|revision|studying)\s*mode|(watch|follow)\s*(my|the)\s*screen|"
    r"let'?s\s*study|start\s*study(ing)?|study\s*with\s*me|help\s*me\s*(study|revise))\b"
)
MORE = re.compile(r"\b(further\s*more|furthermore|go\s*deeper|break\s*it\s*down|explain\s*(that|it|this))\b")


def wanted(text: str) -> str | None:
    """Which study command a sentence is - "on", "off", "more" - or None.

    Short sentences only, like the rest of the built-in commands: "stop studying" is an
    instruction, but a sentence that happens to contain those words is just talk.
    """
    from commands import normalize

    t = normalize(text)
    if len(t.split()) > 8:
        return None
    if OFF.search(t):
        return "off"
    if ON.search(t):
        return "on"
    if MORE.search(t):
        return "more"
    return None


def parse(raw: str) -> dict[str, str]:
    """Pull the three fields out of a reply, tolerating stray markdown or a lost line."""
    out: dict[str, str] = {}
    for key, val in FIELD.findall(raw or ""):
        out.setdefault(key.upper(), val.strip().strip("*_` "))
    return out


def key_of(question: str) -> str:
    """A comparable form of a question, so trivial rewording doesn't read as a new one."""
    return re.sub(r"[^a-z0-9 ]", "", question.lower()).strip()


class Study:
    """The screen watcher. Owns nothing but its own task."""

    def __init__(
        self,
        look: Callable[..., Awaitable[str]],
        say: Callable[[str], Awaitable[None]],
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        quiet: Callable[[], bool] | None = None,
        screen: int | None = None,
    ):
        self.look = look  # Replier.look
        self.say = say  # VoiceLoop.say, which takes the busy lock and so waits its turn
        self.emit = emit
        # "Is it a good moment to look?" - false while he is speaking. See the note up top.
        self.quiet = quiet or (lambda: True)
        self.screen = screen
        self.on = False
        self.task: asyncio.Task[None] | None = None
        self.looks = 0  # model calls spent, for the log line when he stands down
        self.last_key = ""
        self.last_shot: bytes | None = None
        self.last_question = ""
        self.last_answer = ""
        self.last_why = ""

    # ── lifecycle ──
    async def start(self) -> str:
        """Returns what he should say about it (one line, or an explanation of why not)."""
        if not eyes.HAVE_EYES:
            return "I can't see your screen, boss: the screen libraries aren't installed."
        try:
            await eyes.sense(self.screen)
        except eyes.Blind as e:
            log.warning("study mode refused: %s", e)
            return "I can't get at your screen right now, boss."
        if self.on:
            return "Already watching, boss."
        self.on = True
        self.looks = 0
        self.last_key = ""  # so a question already on screen still gets answered
        self.task = asyncio.create_task(self._watch())
        log.info("study mode on (poll %.1fs, moved %.3f, settle %d)", POLL, MOVED, SETTLE)
        return "Watching your screen, boss."

    async def stop(self) -> str:
        self.on = False
        task, self.task = self.task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - cancelling must never raise out
                pass
        log.info("study mode off after %d look(s)", self.looks)
        return "Stopped watching, boss."

    # ── the loop ──
    async def _watch(self) -> None:
        last = None
        still = 0
        moving = True  # start as if something just changed, so the first screen gets read
        last_look = 0.0
        fails = 0
        try:
            while self.on:
                await asyncio.sleep(POLL)
                if not self.quiet():
                    # He's talking, and his own captions are on this screen. Drop the baseline so
                    # whatever his talking changed counts as the new normal, not as a new question.
                    last, still, moving = None, 0, False
                    continue
                try:
                    now = await eyes.sense(self.screen)
                except eyes.Blind as e:
                    log.warning("lost the screen: %s", e)
                    await asyncio.sleep(BACKOFF)
                    continue
                if last is None:
                    last = now
                    continue
                d = eyes.drift(last, now)
                last = now
                if d > MOVED:
                    moving, still = True, 0
                    continue
                if moving:
                    # Something changed and has now held still: read it, but only once it has
                    # held still for SETTLE polls, so a page mid-draw is never read half-done.
                    still += 1
                    if still < SETTLE:
                        continue
                    moving, still = False, 0
                elif time.monotonic() - last_look < SWEEP:
                    continue
                if time.monotonic() - last_look < COOLDOWN:
                    continue
                last_look = time.monotonic()
                try:
                    await self._read()
                    fails = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("study look failed")
                    fails += 1
                    if fails < RETRIES:
                        # The question is still sitting there unanswered, and nothing else will
                        # trigger a retry while the screen holds still. Re-arm instead.
                        moving, still = True, 0
                    await asyncio.sleep(BACKOFF)
        except asyncio.CancelledError:
            pass

    async def _read(self) -> None:
        shot = await eyes.grab(self.screen)
        self.looks += 1
        raw = await self.look(SYSTEM, PROMPT, shot, eyes.MEDIA)
        got = parse(raw)
        question = got.get("QUESTION", "")
        if not question or NONE.match(question):
            log.debug("nothing being asked on screen")
            return
        key = key_of(question)
        if key and key == self.last_key:
            return  # same question, re-rendered; he already said this
        answer = got.get("ANSWER", "").strip()
        if not answer:
            log.info("read a question but no answer: %r", raw[:160])
            return
        why = got.get("WHY", "").strip()
        self.last_key = key
        self.last_shot = shot
        self.last_question, self.last_answer, self.last_why = question, answer, why
        log.info("study: %r -> %r", question[:70], answer[:70])
        await self.emit("study.answer", {"question": question, "answer": answer, "why": why})
        # Answer first, then the short reason. That order is the point of the mode: he hears
        # what it is before he hears why, so a glance is enough and the why is a bonus.
        await self.say(f"{answer}. {why}" if why else answer)

    # ── "further more" ──
    async def further(self) -> str:
        """The full explanation of the last answer, from the same screenshot he read."""
        if not self.last_shot:
            return "Nothing to go further on yet, boss."
        try:
            out = await self.look(
                "You explain a question someone is studying. Speak plainly, no lists, no headings.",
                f"{DEEPER}\n\nThe answer you gave was: {self.last_answer}",
                self.last_shot,
                eyes.MEDIA,
            )
        except Exception as e:
            log.exception("further more failed")
            return f"Couldn't get into it, boss: {str(e)[:80]}"
        return out.strip() or "Nothing more to add, boss."
