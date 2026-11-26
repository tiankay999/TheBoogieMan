"""The voice loop: listen → transcribe → reply → speak, emitting state to the UI.

Everything it touches (mic, speaker, STT, TTS, replier) is injected so it can be
tested without hardware or network.

Events (pushed to the UI, no id):
  voice.state      {state: idle|listening|hearing|transcribing|thinking|speaking|muted|error, detail?}
  voice.level      {source: mic|voice, level: 0..1}
  voice.spectrum   {source: mic|voice, bands: [24 x 0..1]}  (the voice pattern)
  voice.transcript {text}
  voice.reply      {text}

Barge-in: while he speaks the mic stays open. If you talk over him (louder than the echo of
his own voice, for ~1/5 s) he stops mid-sentence and listens. "Pause" / "stop" / "wait" just
keeps him quiet; "continue" / "go on" picks up roughly where he stopped.

The gate that decides "you" from "his own voice coming back" is the delicate part, and it was
set far too high: measured against real playback it sat at 0.316 while conversational speech
into a laptop mic is about 0.10, so only a shout got through. See the BARGE_* constants.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

import numpy as np

from audio import MIC_RATE, Segmenter, rms, spectrum, to_wav

log = logging.getLogger("brain.voice")

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]

CHUNK = 1024  # playback block (~43 ms at 24 kHz): one level update each
BARGE_IN = os.environ.get("BOOGEYMAN_BARGE_IN", "1").lower() not in ("0", "off", "false", "no")
# Higher = you must talk louder to interrupt him. Raise it if he cuts himself off on loud speakers.
BARGE_SENSITIVITY = float(os.environ.get("BOOGEYMAN_BARGE_LEVEL", "1.0"))
BARGE_FRAMES = 6  # ~180 ms of you talking over him
# How much of his own voice is assumed to come back through the mic before anything is known.
# This starts low on purpose. The two ways of being wrong are not symmetric: too sensitive and
# he clips himself, which you hear immediately and the learner below corrects within a second;
# too insensitive and he simply never stops, which looks like the feature doesn't exist - and
# it never recovers, because the learner only runs on frames that were NOT a barge-in. So err
# towards interrupting. It was 0.6, which put the gate above a raised voice from the start.
BARGE_COUPLING0 = 0.15
BARGE_MULT = 1.8  # safety factor over the expected echo; was 2.5
BARGE_COUPLING_MAX = 1.5  # was 3.0, which could pin the gate at 1.58 - unreachable forever
# Absolute ceiling, whatever the echo learner believes. Measured against real playback, speech
# into a laptop mic sits near 0.10 and a clearly raised voice near 0.20, so a gate above this
# cannot be cleared by a human being and the feature is dead. Scales with BARGE_SENSITIVITY so
# raising that for loud speakers still lifts the ceiling with it.
BARGE_CEILING = float(os.environ.get("BOOGEYMAN_BARGE_CEILING", "0.18"))
CHARS_PER_SEC = 14.0  # rough speaking rate, to find where he stopped

PAUSE = re.compile(r"^\W*(pause|stop|wait|hold\s*on|hold\s*up|shh+|quiet|be\s*quiet|shut\s*up|one\s*sec(ond)?|enough|ok(ay)?\s*stop)\b", re.I)
CONTINUE = re.compile(r"\b(continue|go\s*on|carry\s*on|keep\s*going|resume|as\s*you\s*were\s*saying|where\s*were\s*we|finish\s*(what\s*you\s*were\s*saying|that))\b", re.I)


class MicLike(Protocol):
    async def read(self) -> np.ndarray: ...
    def drain(self) -> None: ...


class SpeakerLike(Protocol):
    def write(self, pcm: np.ndarray) -> None: ...


def boost(level: float) -> float:
    """Map raw RMS (speech is ~0.02-0.3) onto a lively 0..1 for the visuals."""
    return float(min(1.0, (level * 6.0) ** 0.7))


class VoiceLoop:
    def __init__(
        self,
        emit: Emit,
        mic: MicLike | None,
        speaker: SpeakerLike | None,
        transcribe: Callable[[bytes], Awaitable[str]] | None,
        tts: Callable[[str], AsyncIterator[bytes]] | None,
        reply: Callable[[str], Awaitable[str]],
        tts_rate: int = 24000,
        after_turn: Callable[[], Awaitable[None]] | None = None,
        reply_stream: Callable[[str], AsyncIterator[str]] | None = None,
        tts_obj: Any | None = None,
    ):
        self.tts_rate = tts_rate
        self.after_turn = after_turn  # runs once the reply has been spoken (e.g. shutdown)
        self.emit = emit
        self.mic = mic
        self.speaker = speaker
        self.transcribe = transcribe
        self.tts = tts
        self.reply = reply
        # Together these two enable the live path: a brain that can hand text over in pieces and
        # a voice that can synthesize one piece at a time. Without both, he buffers as before.
        self.reply_stream = reply_stream
        self.tts_obj = tts_obj
        self.muted = False
        self.busy = asyncio.Lock()
        self.seg = Segmenter()
        # Barge-in
        self.barge_in = BARGE_IN
        self.speaking = False
        self.barged = False  # you interrupted; capturing what you say
        self.interrupt = asyncio.Event()
        self.recent: deque[np.ndarray] = deque(maxlen=14)  # ~400 ms of mic while he talks
        self.play_levels: deque[float] = deque(maxlen=5)  # what he just played (echo reference)
        self.coupling = BARGE_COUPLING0  # learned: how loud his own voice comes back
        self.barge_n = 0
        self.unsaid: str | None = None  # the rest of a reply you cut off
        self.tasks: set[asyncio.Task] = set()

    @property
    def can_listen(self) -> bool:
        return self.mic is not None and self.transcribe is not None

    async def state(self, state: str, **extra: Any) -> None:
        await self.emit("voice.state", {"state": state, **extra})

    async def run(self) -> None:
        """Listen forever. While he talks the mic watches for you interrupting; otherwise it's ignored while busy."""
        if not self.can_listen:
            await self.state("idle", detail="mic or speech-to-text unavailable")
            return
        await self.state("listening")
        tick = 0
        while True:
            frame = await self.mic.read()  # type: ignore[union-attr]
            if self.muted:
                continue
            if self.busy.locked() and not self.barged:
                if self.speaking and self.barge_in:
                    await self.watch_barge(frame)
                continue
            tick += 1
            if tick % 2 == 0:  # ~16 mic updates per second is plenty
                await self.emit("voice.level", {"source": "mic", "level": boost(rms(frame))})
                await self.emit("voice.spectrum", {"source": "mic", "bands": spectrum(frame, MIC_RATE)})
            was = self.seg.speaking
            utterance = self.seg.feed(frame)
            if self.seg.speaking and not was:
                await self.state("hearing")
            if utterance is not None:
                self.barged = False
                task = asyncio.create_task(self.handle_utterance(utterance))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                await asyncio.sleep(0)  # let the turn take the lock before the next frame
            elif was and not self.seg.speaking:
                self.barged = False
                if not self.busy.locked():
                    await self.state("listening")  # too short, discarded

    async def watch_barge(self, frame: np.ndarray) -> None:
        """While he speaks: is that you talking over him, or just his own voice coming back?"""
        self.recent.append(frame)
        level = rms(frame)
        play = max(self.play_levels, default=0.0)
        gate = max(self.seg.threshold * 4.0, self.coupling * play * BARGE_MULT, 0.02)
        gate = min(gate, BARGE_CEILING) * BARGE_SENSITIVITY
        if level > gate:
            self.barge_n += 1
        else:
            # Decay rather than reset. Real speech dips between words and syllables, and
            # zeroing the count on one quiet frame meant he needed 180 ms of unbroken volume
            # that ordinary talking never provides.
            self.barge_n = max(0, self.barge_n - 1)
            if play > 0.01:  # learn the echo from frames that are clearly just him
                self.coupling = min(BARGE_COUPLING_MAX, self.coupling * 0.97 + (level / play) * 0.03)
        if self.barge_n >= BARGE_FRAMES:
            log.info("barge-in: you talked over him (mic %.3f > %.3f)", level, gate)
            self.barge_n = 0
            self.barged = True
            self.interrupt.set()
            # Start capturing with what you've already said.
            self.seg.reset()
            self.seg.speaking = True
            self.seg.frames = list(self.recent)
            self.recent.clear()
            await self.state("hearing")

    async def handle_utterance(self, pcm: np.ndarray) -> None:
        async with self.busy:
            try:
                await self.state("transcribing")
                text = (await self.transcribe(to_wav(pcm))).strip()  # type: ignore[misc]
                if len(text) < 2 or text.startswith("("):
                    return  # noise, not words
                log.info("heard: %r", text)
                await self.emit("voice.transcript", {"text": text})
                if len(text.split()) <= 4 and PAUSE.search(text):
                    log.info("paused")  # stay quiet; "continue" picks it up
                    return
                if self.unsaid and len(text.split()) <= 6 and CONTINUE.search(text):
                    rest, self.unsaid = self.unsaid, None
                    await self.emit("voice.reply", {"text": rest})
                    await self.say(rest, locked=True)
                    return
                self.unsaid = None
                await self.respond(text, locked=True)
            except Exception as e:  # keep listening whatever happens
                log.exception("voice turn failed")
                await self.state("error", detail=str(e)[:160])
                await asyncio.sleep(1.5)
            finally:
                if not self.barged:  # (if you cut in, we're already capturing you)
                    self.seg.reset()
                    if self.mic:
                        self.mic.drain()  # drop anything heard while we were busy (incl. our own voice)
                    await self.state("muted" if self.muted else "listening")

    async def respond(self, text: str, locked: bool = False) -> str:
        """Think of a reply to `text`, then say it."""
        if not locked:
            async with self.busy:
                return await self.respond(text, locked=True)
        await self.state("thinking")
        piece = getattr(self.tts_obj, "piece", None) if self.tts_obj else None
        if self.reply_stream and piece and self.speaker:
            out = await self.respond_live(text, piece)
        else:
            out = await self.reply(text)
            log.info("reply: %r", out[:160])
            await self.emit("voice.reply", {"text": out})
            if out.strip():
                await self.say(out, locked=True)
        if self.after_turn:
            await self.after_turn()
        return out

    async def respond_live(self, text: str, piece) -> str:
        """Think and talk at the same time.

        Waiting for the last token before synthesizing the first word put a second and a half of
        silence in front of every answer. Here the opening clause is sent to be spoken as soon as
        it is written, and the rest of the thinking happens behind his own voice.
        """
        from freevoice import speak_live

        said: list[str] = []
        shown = False

        async def written() -> AsyncIterator[str]:
            nonlocal shown
            async for t in self.reply_stream(text):  # type: ignore[misc]
                said.append(t)
                yield t
            out = "".join(said).strip()
            if out:
                # He is still talking; put the text on screen now rather than when he finishes.
                log.info("reply: %r", out[:160])
                await self.emit("voice.reply", {"text": out})
                shown = True

        played = await self._speak(speak_live(piece, written()))
        out = "".join(said).strip()
        if out and not shown:  # cut off before the brain finished
            log.info("reply (cut short): %r", out[:160])
            await self.emit("voice.reply", {"text": out})
        if self.interrupt.is_set():
            self.unsaid = rest_of(out, played / self.tts_rate)
            log.info("interrupted after %.1fs; %d chars unsaid", played / self.tts_rate, len(self.unsaid or ""))
        return out

    async def say(self, text: str, locked: bool = False) -> None:
        if not locked:
            async with self.busy:
                return await self.say(text, locked=True)
        if not (self.tts and self.speaker):
            return
        played = await self._speak(self.tts(text))
        if self.interrupt.is_set():
            self.unsaid = rest_of(text, played / self.tts_rate)
            log.info("interrupted after %.1fs; %d chars unsaid", played / self.tts_rate, len(self.unsaid or ""))

    async def _speak(self, pcm: AsyncIterator[bytes]) -> int:
        """Play audio as it arrives, watching the mic for you talking over him.

        Returns the number of samples played, which is how far through the reply he actually got.
        """
        await self.state("speaking")
        self.interrupt.clear()
        self.play_levels.clear()
        self.recent.clear()
        self.barge_n = 0
        self.speaking = True
        played = 0
        buf = b""
        try:
            async for chunk in pcm:
                buf += chunk
                usable = len(buf) - (len(buf) % (CHUNK * 2))
                for i in range(0, usable, CHUNK * 2):
                    if self.interrupt.is_set():
                        break
                    block = np.frombuffer(buf[i : i + CHUNK * 2], dtype=np.int16)
                    await self.play_block(block)
                    played += len(block)
                buf = buf[usable:]
                if self.interrupt.is_set():
                    break
            if len(buf) >= 2 and not self.interrupt.is_set():
                await self.play_block(np.frombuffer(buf[: len(buf) - len(buf) % 2], dtype=np.int16))
        except Exception as e:
            # Losing his voice shouldn't look like a crash. Say why, on screen and in the log,
            # and keep listening: the reply itself was already sent to the UI as text.
            detail = tts_failure(e)
            log.warning("text-to-speech failed: %s", detail)
            await self.state("error", detail=detail)
        finally:
            self.speaking = False
            await self.emit("voice.level", {"source": "voice", "level": 0.0})
        return played

    async def play_block(self, pcm: np.ndarray) -> None:
        self.play_levels.append(rms(pcm))
        await self.emit("voice.level", {"source": "voice", "level": boost(rms(pcm))})
        await self.emit("voice.spectrum", {"source": "voice", "bands": spectrum(pcm, self.tts_rate)})
        await asyncio.to_thread(self.speaker.write, pcm)  # type: ignore[union-attr]

    async def set_muted(self, muted: bool) -> None:
        self.muted = muted
        self.seg.reset()
        if self.mic:
            self.mic.drain()
        if not self.busy.locked():
            await self.state("muted" if muted else "listening")


def tts_failure(e: Exception) -> str:
    """Turn a text-to-speech exception into something worth putting on screen."""
    s = str(e)
    if "quota_exceeded" in s or "credits remaining" in s:
        return "ElevenLabs credits used up: no voice until the quota resets"
    if "missing_permissions" in s:
        return "the ElevenLabs key is missing the text_to_speech permission"
    if "401" in s or "403" in s:
        return "ElevenLabs rejected the key"
    if "Timeout" in type(e).__name__ or "Connect" in type(e).__name__:
        return "couldn't reach ElevenLabs: check the internet"
    return f"{type(e).__name__}: {s[:120]}"


def rest_of(text: str, seconds: float) -> str | None:
    """Roughly what he hadn't said yet after `seconds`, starting at a sentence or clause boundary."""
    cut = int(seconds * CHARS_PER_SEC)
    if cut >= len(text) - 3:
        return None
    window = text[: cut + 1]
    best = max(window.rfind(p) for p in (". ", "? ", "! ", ", ", "; ", ": "))
    start = best + 2 if best > 0 else 0
    rest = text[start:].strip()
    return rest or None
