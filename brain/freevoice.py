"""The free voice stack: ears and a voice that cost nothing and need no extra key.

  Ears  GroqSTT  - Whisper large v3 turbo on Groq, using the GROQ_API_KEY he already has.
  Voice EdgeTTS  - Microsoft Edge's neural voices. No key at all, 300+ voices.
        GroqTTS  - Orpheus (open source, Apache-2.0) on Groq. Quicker off the mark than Edge,
                   but the model's terms must be accepted once at console.groq.com first.

All of them match the ElevenLabs interface (`transcribe(wav) -> str`,
`speak(text) -> AsyncIterator[bytes]`), so VoiceLoop can't tell the difference.

Latency here is measured, not guessed. Edge charges about 1.6 s per *request* almost regardless
of how much text is in it (1.15 s for 17 characters, 1.69 s for 104), and that cost is Microsoft
synthesizing, not a connection being set up - pooling a keep-alive connection across turns came
out slower than reconnecting, so it isn't done. What the fixed-cost-per-request shape does mean
is that the opening piece should be small and later pieces large: see split_first and chunks.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import wave
from collections.abc import AsyncIterator

import httpx
import numpy as np

from reply import CONNECT_RETRIES, CONNECT_TIMEOUT, GROQ_URL, KEEPALIVE_S

log = logging.getLogger("brain.freevoice")

TTS_RATE = 24000  # what Edge gives us, and what the Speaker is opened at
GROQ_STT_MODEL = os.environ.get("GROQ_STT_MODEL", "whisper-large-v3-turbo")
# Whisper leans hard on context, and out of the box it hears "I dey here" as "ID here".
# Seeding it with the words this boss actually uses fixes most of that for free.
STT_PROMPT = os.environ.get(
    "GROQ_STT_PROMPT",
    "Ghanaian Pidgin and English. Words you will hear: chale, boss, abeg, dey, I dey, you dey, "
    "e dey bee, how far, make I, wahala, small small, no wahala, jollof, cedis, Accra, Boogeyman, "
    "devil mode, work mode.",
)

# This started as a Nigerian voice, on the theory that a West African one would carry
# Ghanaian Pidgin better. It doesn't: Edge has no Ghanaian voice at all, and a Nigerian
# accent reading Ghanaian Pidgin lands worse than a neutral one, so there is no accent
# to match here and the pick is pace plus taste.
# Override with EDGE_VOICE; `edge-tts --list-voices` shows all 300-odd.
DEFAULT_VOICE = os.environ.get("EDGE_VOICE", "en-US-BrianMultilingualNeural")
DEVIL_VOICE = os.environ.get("EDGE_DEVIL_VOICE") or DEFAULT_VOICE
# A voice's reading pace is its own, and the spread is wide: measured over one reply at
# +0%, the English male voices run from 17.4 chars/sec (Brian and Andrew Multilingual,
# the newest generation) down to 12.0 (Prabhat). Steffan sat at 14.9, eleventh of 23.
# That spread is worth more than the rate knob, because a slow voice cranked far enough
# to keep up starts to sound artificial: Steffan needed +53% to reach the pace Brian
# Multilingual reaches at +30%. Pick a quick reader first, then push it gently.
# Re-measure if you change EDGE_VOICE - this number belongs to the voice, not to him.
DEFAULT_RATE = os.environ.get("EDGE_RATE", "+40%")
# A single Edge request occasionally hangs far past its usual 1.6 s. Give up and ask again.
EDGE_TIMEOUT = float(os.environ.get("EDGE_TIMEOUT", "6"))
EDGE_TRIES = int(os.environ.get("EDGE_TRIES", "3"))
# Devil mode: slower and pitched down, relative to the livelier default above.
DEVIL_RATE = os.environ.get("EDGE_DEVIL_RATE", "+4%")
DEVIL_PITCH = os.environ.get("EDGE_DEVIL_PITCH", "-25Hz")

# Orpheus on Groq. The hosted voices are NOT the open-source model card's list
# (tara/leo/zac/...): Groq serves autumn, diana, hannah, austin, daniel and troy. Asking for
# one of the others is a 400, which is how this was found.
GROQ_TTS_MODEL = os.environ.get("GROQ_TTS_MODEL", "canopylabs/orpheus-v1-english")
GROQ_TTS_VOICE = os.environ.get("GROQ_TTS_VOICE", "austin")
GROQ_TTS_DEVIL_VOICE = os.environ.get("GROQ_TTS_DEVIL_VOICE", "daniel")

FIRST_SENTENCE_MIN = 8  # a finished sentence is worth saying at almost any length
FIRST_MIN = 25  # a mid-sentence comma is not: don't open with a bare "Chale," and stop
FIRST_MAX = 70  # the opening piece is cut short: it is the only wait you actually hear
MIN_CHUNK = 140  # later pieces are prefetched behind playback, so make them big and smooth
LOOKAHEAD = 2  # pieces synthesized ahead of playback, so there are no gaps
SENTENCE = re.compile(r"(?<=[.!?…])\s+|\n+")
CLAUSE = re.compile(r"(?<=[,;:])\s+|\s+[—–]\s+")


def transport() -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(
        retries=CONNECT_RETRIES,
        limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=KEEPALIVE_S),
    )


def split_first(text: str) -> tuple[str, str]:
    """Cut an opening piece of at most FIRST_MAX characters off `text`, on a clause boundary.

    Edge costs about the same for a short request as a long one, so the opening piece is made
    small: it is the one wait he cannot hide behind playback. The rest is synthesized while the
    first words are already coming out of the speaker.
    """
    if len(text) <= FIRST_MAX:
        return text, ""
    cut = 0
    for m in CLAUSE.finditer(text):
        if m.end() > FIRST_MAX:
            break
        cut = m.end()
    if not cut:  # no comma to lean on; fall back to the last word boundary
        sp = text.rfind(" ", 0, FIRST_MAX)
        cut = sp + 1 if sp > FIRST_MAX // 2 else 0
    if not cut:
        return text, ""
    return text[:cut].strip(), text[cut:].strip()


def chunks(text: str) -> list[str]:
    """Sentence-sized pieces, smallest first, so the first words play while the rest is made."""
    parts = [p.strip() for p in SENTENCE.split(text.strip()) if p.strip()]
    if not parts:
        return []
    head, tail = split_first(parts[0])
    out = [head]
    for part in ([tail] if tail else []) + parts[1:]:
        if len(out) > 1 and len(out[-1]) < MIN_CHUNK:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out


def decode_mp3(mp3: bytes) -> bytes:
    """MP3 -> raw PCM16 mono at TTS_RATE. Runs in a thread; miniaudio releases the GIL."""
    import miniaudio

    dec = miniaudio.decode(mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=TTS_RATE)
    return np.asarray(dec.samples, dtype=np.int16).tobytes()


def decode_wav(buf: bytes) -> bytes:
    """WAV -> raw PCM16 mono at TTS_RATE, resampling if the provider picked another rate.

    The header is not trusted for length. Groq streams Orpheus with a placeholder data size of
    0x7FFFFFFF, so getnframes() claims 89,478 seconds of audio for a one-second clip; handing
    that straight to readframes() asks it for a 4 GB read and only works because the buffer
    runs out first. Reading until it stops giving anything is the same answer without the trap.
    """
    with wave.open(io.BytesIO(buf), "rb") as w:
        rate, chans = w.getframerate(), w.getnchannels()
        got = []
        while True:
            frames = w.readframes(16384)
            if not frames:
                break
            got.append(frames)
        pcm = np.frombuffer(b"".join(got), dtype=np.int16)
    if chans > 1:
        pcm = pcm.reshape(-1, chans).mean(axis=1).astype(np.int16)
    if rate != TTS_RATE and pcm.size:
        n = int(round(pcm.size * TTS_RATE / rate))
        pcm = np.interp(np.linspace(0, pcm.size - 1, n), np.arange(pcm.size), pcm).astype(np.int16)
    return pcm.tobytes()


def next_piece(buf: str, first: bool, done: bool) -> tuple[str, str]:
    """Pull one speakable piece off the front of `buf`, for text that is still arriving.

    Returns (piece, rest). An empty piece means "not enough text to cut safely yet" - unless
    `done`, in which case whatever is left is spoken as it stands.
    """
    if not buf.strip():
        return "", "" if done else buf
    if first:
        # The opening piece is cut at the earliest decent boundary, because this is the only
        # silence the boss actually sits through. A finished sentence is always worth sending,
        # however short - "I dey fine, chale." is a whole answer. A mid-sentence comma needs
        # more behind it, so he doesn't open with a bare "Chale," and then stop dead.
        ends: dict[int, bool] = {m.end(): True for m in SENTENCE.finditer(buf)}
        for m in CLAUSE.finditer(buf):
            ends.setdefault(m.end(), False)
        for b in sorted(ends):
            if b >= (FIRST_SENTENCE_MIN if ends[b] else FIRST_MIN):
                return buf[:b].strip(), buf[b:]
        if done:
            return buf.strip(), ""
        if len(buf) >= FIRST_MAX:  # a long opening clause with no comma to lean on
            sp = buf.rfind(" ", 0, FIRST_MAX)
            if sp > FIRST_MIN:
                return buf[:sp].strip(), buf[sp:]
        return "", buf
    # Later pieces are hidden behind playback, so prefer big ones: better prosody, fewer requests.
    for m in SENTENCE.finditer(buf):
        if m.end() >= MIN_CHUNK:
            return buf[:m.end()].strip(), buf[m.end():]
    if done:
        return buf.strip(), ""
    if len(buf) >= MIN_CHUNK * 2:  # rambling on with no full stop; cut at a clause instead
        for m in CLAUSE.finditer(buf):
            if m.end() >= MIN_CHUNK:
                return buf[:m.end()].strip(), buf[m.end():]
    return "", buf


async def speak_live(piece, text_iter: AsyncIterator[str]) -> AsyncIterator[bytes]:
    """Speak a reply while the brain is still writing it.

    The brain's last token and the voice's first request used to be strictly sequential, which
    cost about a second and a half of silence on every single turn. Here the text is drained in
    the background, so the opening clause goes off to be synthesized the moment it is complete
    and the rest of the thinking happens behind the first words.
    """
    parts: list[str] = []
    finished = asyncio.Event()
    arrived = asyncio.Event()

    async def pump() -> None:
        try:
            async for t in text_iter:
                if t:
                    parts.append(t)
                    arrived.set()
        except Exception:
            log.exception("reply stream died while he was talking")
        finally:
            finished.set()
            arrived.set()

    pumping = asyncio.create_task(pump())
    flight: list[asyncio.Task[bytes]] = []
    buf, first = "", True
    try:
        while True:
            if parts:  # whatever the brain wrote while the last piece was being synthesized
                buf += "".join(parts)
                parts.clear()
            done = finished.is_set()
            while len(flight) < LOOKAHEAD:
                p, buf = next_piece(buf, first, done)
                if not p:
                    break
                first = False
                flight.append(asyncio.create_task(piece(p)))
            if flight:
                pcm = await flight.pop(0)
                if pcm:
                    yield pcm
                continue  # round again: more text may have landed during that synthesis
            if done:
                return
            arrived.clear()
            await arrived.wait()
    finally:  # barge-in closes the generator early; don't leave anything running
        pumping.cancel()
        for t in flight:
            t.cancel()


async def pipeline(synth, pieces: list[str]) -> AsyncIterator[bytes]:
    """Run `synth` over `pieces` ahead of playback, so only the first piece is ever waited for.

    Playback is real time and both providers synthesize faster than that, so keeping a couple of
    pieces in flight means he never pauses in the middle of a reply.
    """
    flight: list[asyncio.Task[bytes]] = []
    try:
        for i in range(len(pieces)):
            if i == 0:
                # The first piece runs alone. Prefetching alongside it shares the link and
                # pushes time-to-first-word out by well over a second, which is exactly the
                # silence we are trying to remove.
                flight.append(asyncio.create_task(synth(pieces[0])))
            else:
                while len(flight) < min(LOOKAHEAD, len(pieces) - i):
                    flight.append(asyncio.create_task(synth(pieces[i + len(flight)])))
            pcm = await flight.pop(0)
            if pcm:
                yield pcm
    finally:  # barge-in closes the generator early; don't leave synthesis running
        for t in flight:
            t.cancel()


class EdgeTTS:
    """Microsoft Edge neural voices. Free, no key, no account.

    Its ~1.6 s is server-side synthesis, not connection setup: pooling a keep-alive connection
    across turns was measured at 1876 ms average against 1712 ms for a fresh connection each
    time, so there is nothing to win there and the pooling was taken back out.
    """

    def __init__(self, voice: str = DEFAULT_VOICE, devil_voice: str = DEVIL_VOICE):
        self.voice = voice
        self.devil_voice = devil_voice
        self.devil = False

    @property
    def label(self) -> str:
        return f"edge:{self.devil_voice if self.devil else self.voice}"

    async def _mp3(self, text: str) -> bytes:
        import edge_tts

        kw = {"rate": DEVIL_RATE, "pitch": DEVIL_PITCH} if self.devil else {"rate": DEFAULT_RATE}
        comm = edge_tts.Communicate(text, self.devil_voice if self.devil else self.voice, **kw)
        buf = bytearray()
        async for part in comm.stream():
            if part["type"] == "audio" and part.get("data"):
                buf += part["data"]
        return bytes(buf)

    async def piece(self, text: str) -> bytes:
        """Synthesize one piece, abandoning the request if Edge stalls.

        Edge normally answers in about 1.6 s but every so often a single request hangs for 15 s
        or more, and that one stall is the difference between an assistant that feels quick and
        one that feels broken. How much text is in the request barely affects how long it takes
        (1.8 s for 17 characters, 1.7 s for 104), so a flat cap is safe, and a fresh request
        almost always comes straight back - far better than sitting on a dead one.
        """
        for attempt in range(1, EDGE_TRIES + 1):
            try:
                mp3 = await asyncio.wait_for(self._mp3(text), EDGE_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                log.warning("edge stalled past %.0fs, retrying (%d/%d)", EDGE_TIMEOUT, attempt, EDGE_TRIES)
                continue
            return await asyncio.to_thread(decode_mp3, mp3) if mp3 else b""
        raise TimeoutError(f"Edge didn't answer in {EDGE_TRIES} tries of {EDGE_TIMEOUT:.0f}s")

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        async for pcm in pipeline(self.piece, chunks(text)):
            yield pcm


class GroqTTS:
    """Orpheus on Groq: open source, quicker to start, slower to finish, and rate-limited.

    Opt-in with BOOGEYMAN_TTS=groq. It is not the default, and the measurements are why. On the
    same short reply, against Edge on en-US-BrianMultilingualNeural at +40%:

        synth        954 ms   vs  1560 ms   <- Orpheus starts ~600 ms sooner
        speech      4.96 s    vs  3.53 s    <- and then talks 1.43 s longer
        total       5.91 s    vs  5.09 s    <- so it loses by ~800 ms per reply

    Orpheus reads at 9.5 characters a second on that line against Edge's 13.3, and it cannot be
    pushed: the `speed` parameter is accepted with a 200 and silently ignored - 0.5, 1.5 and the
    default all return byte-identical audio. Edge's rate knob is the thing that made the voice
    quick, and Orpheus has no equivalent.

    The free tier also allows only 100 requests per four-hour window, and speak_live spends two
    to four of those per reply, so a study session would exhaust it in well under an hour.

    The model's terms must be accepted once per org at console.groq.com before any key may call
    it; until then every request is a 400 with code model_terms_required.
    """

    def __init__(
        self,
        api_key: str,
        voice: str = GROQ_TTS_VOICE,
        devil_voice: str = GROQ_TTS_DEVIL_VOICE,
        model: str = GROQ_TTS_MODEL,
        http: httpx.AsyncClient | None = None,
    ):
        self.voice = voice
        self.devil_voice = devil_voice
        self.model = model
        self.devil = False
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=CONNECT_TIMEOUT), transport=transport()
        )
        self.headers = {"Authorization": f"Bearer {api_key}"}

    @property
    def label(self) -> str:
        return f"groq:orpheus:{self.devil_voice if self.devil else self.voice}"

    async def piece(self, text: str) -> bytes:
        r = await self.http.post(
            f"{GROQ_URL}/audio/speech",
            headers=self.headers,
            json={
                "model": self.model,
                "input": text,
                "voice": self.devil_voice if self.devil else self.voice,
                "response_format": "wav",
            },
        )
        r.raise_for_status()
        return await asyncio.to_thread(decode_wav, r.content)

    async def available(self) -> bool:
        """Is this key allowed to use the model yet? One cheap request, asked once at boot."""
        try:
            await self.piece("ready")
            return True
        except Exception as e:
            log.info("Orpheus not available (%s); staying on Edge", str(e)[:160])
            return False

    async def warm(self) -> None:
        try:
            await self.piece("ready")
        except Exception as e:
            log.debug("orpheus warm-up skipped: %s", e)

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        async for pcm in pipeline(self.piece, chunks(text)):
            yield pcm


class GroqSTT:
    """Whisper on Groq. Free tier, and it reuses the brain key that's already configured."""

    def __init__(
        self,
        api_key: str,
        model: str = GROQ_STT_MODEL,
        http: httpx.AsyncClient | None = None,
        prompt: str = STT_PROMPT,
    ):
        self.model = model
        self.prompt = prompt
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(45.0, connect=CONNECT_TIMEOUT), transport=transport()
        )
        self.headers = {"Authorization": f"Bearer {api_key}"}

    async def transcribe(self, wav: bytes) -> str:
        r = await self.http.post(
            f"{GROQ_URL}/audio/transcriptions",
            headers=self.headers,
            data={"model": self.model, "response_format": "json", "temperature": "0", "prompt": self.prompt},
            files={"file": ("speech.wav", wav, "audio/wav")},
        )
        r.raise_for_status()
        return str(r.json().get("text", "")).strip()


def is_permanent(e: Exception) -> bool:
    """Worth giving up on this provider for the session, rather than retrying it every turn."""
    s = str(e)
    return any(k in s for k in ("quota_exceeded", "credits remaining", "missing_permissions", "401", "403"))


class FallbackSTT:
    """Hears with `primary`; the first time it fails for good, switches to `backup` for the session."""

    def __init__(self, primary, backup, on_switch=None):
        self.primary = primary
        self.backup = backup
        self.on_switch = on_switch
        self.switched = False

    async def transcribe(self, wav: bytes) -> str:
        if not self.switched:
            try:
                return await self.primary.transcribe(wav)
            except Exception as e:
                if not is_permanent(e):
                    raise
                self.switched = True
                log.warning("primary ears gave up (%s); switching to the free one", str(e)[:120])
                if self.on_switch:
                    self.on_switch(str(e))
        return await self.backup.transcribe(wav)


class FallbackTTS:
    """Speaks with `primary`; the first time it fails for good, switches to `backup` and stays there.

    Only falls back before any audio has been yielded: once he has started talking, restarting
    the sentence on another voice would stutter and repeat him.
    """

    def __init__(self, primary, backup, on_switch=None):
        self.primary = primary
        self.backup = backup
        self.on_switch = on_switch
        self.switched = False
        self._devil = False

    @property
    def devil(self) -> bool:
        return self._devil

    @devil.setter
    def devil(self, on: bool) -> None:
        self._devil = on
        for t in (self.primary, self.backup):
            if hasattr(t, "devil"):
                t.devil = on

    @property
    def label(self) -> str:
        return getattr(self.live, "label", type(self.live).__name__)

    @property
    def live(self):
        return self.backup if self.switched else self.primary

    @property
    def piece(self):
        """The live voice's single-piece synthesis, or None if it can't do one piece at a time.

        ElevenLabs can't, so while it is primary the caller buffers the whole reply and goes
        through speak() - which is also what performs the switch. Once switched, Edge takes over
        and this starts answering, so later turns get the live streaming path for free.
        """
        return getattr(self.live, "piece", None)

    async def warm(self) -> None:
        """Warm the backup too: it is the one that will still be cold when it's needed."""
        for t in (self.primary, self.backup):
            if hasattr(t, "warm"):
                await t.warm()

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        if not self.switched:
            started = False
            try:
                async for c in self.primary.speak(text):
                    started = True
                    yield c
                return
            except Exception as e:
                if started or not is_permanent(e):
                    raise  # mid-sentence, or a blip worth retrying next turn
                self.switched = True
                log.warning("primary voice gave up (%s); switching to %s for this session", str(e)[:120], self.label)
                if self.on_switch:
                    self.on_switch(str(e))
        async for c in self.backup.speak(text):
            yield c
