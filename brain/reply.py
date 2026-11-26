"""What the Boogeyman says back.

Pluggable LLM provider:
  - anthropic: Claude (ANTHROPIC_API_KEY)
  - groq:      Groq's free tier, OpenAI-compatible (GROQ_API_KEY)
  - openai:    any OpenAI-compatible endpoint (LLM_BASE_URL + LLM_API_KEY + LLM_MODEL),
               e.g. Gemini or OpenRouter
With no key at all he falls back to a few built-in lines.

Personality comes from persona.md (+ optional boss.md); long-term memory from memory.py.
The model can call tools (remember / forget) in a short loop before answering.
"""

from __future__ import annotations

import base64
import json
import os
import time
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

try:
    # The Anthropic SDK bundles its own HTTP stack (httpx2), so its failures are NOT httpx
    # exceptions and would otherwise escape reply()'s handlers and kill the whole voice turn.
    from anthropic import APIStatusError, AnthropicError
except ImportError:  # anthropic is optional; an empty tuple simply never matches
    APIStatusError = AnthropicError = ()  # type: ignore[assignment,misc]

from memory import Memory

log = logging.getLogger("brain.reply")

HERE = Path(__file__).parent
FALLBACK_GREETINGS = ["Hello, boss.", "I'm here, boss.", "Hey, boss. I'm listening."]
GROQ_URL = "https://api.groq.com/openai/v1"
MAX_TOOL_ROUNDS = 4
# He was answering "how far boss" with 210 characters: eleven seconds of talking at a man who
# asked how his day was. persona.md already asks for 1-3 sentences and he drifts off it anyway,
# so this goes last in the system prompt, where recency does the arguing.
BREVITY = (
    "## Length (this matters most)\n"
    "He is listening, not reading. Every extra sentence is time he spends waiting on you.\n"
    "- Answer in one or two short sentences, then stop.\n"
    "- Don't restate his question, don't narrate what you're about to do, don't list your own\n"
    "  abilities, and don't end by offering more help. He'll ask.\n"
    "- Go long only when he actually asks for something long."
)
# Enough for the two sentences asked for above, with room to finish a thought. It is a backstop
# against a runaway monologue, not the real limit: that's BREVITY.
VOICE_TOKENS = 400
# A screen answer is a verdict plus one line of why, so it needs less room than a chat reply.
VISION_TOKENS = int(os.environ.get("BOOGEYMAN_VISION_TOKENS", "200"))
# Groq's text models are blind, so a look can't ride the GROQ_FALLBACKS chain.
GROQ_VISION = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
# Claude thinks before it answers unless told otherwise, and on a voice turn that is pure delay.
# Measured on "how far boss": 146 output tokens of deliberation to produce 55 characters of
# "doing fine, ready when you are", and 748 ms of silence before the first word - half the wait
# on a short turn. Chatting doesn't need it, and turning it off also retires the "invalid
# signature in thinking block" failure that used to kill a turn mid-tool-call. The research
# agent keeps its own call, and its reasoning, untouched. BOOGEYMAN_THINK=1 puts it back.
THINK = os.environ.get("BOOGEYMAN_THINK", "0").lower() not in ("0", "off", "false", "no")
NO_THINKING: dict[str, Any] = {} if THINK else {"thinking": {"type": "disabled"}}
HISTORY_TO_LLM = int(os.environ.get("BOOGEYMAN_HISTORY", "12"))  # messages of recent chat sent with each turn
# Groq's free tier limits each model separately (per minute and per day). When one is used up,
# he moves on to the next instead of going quiet.
GROQ_FALLBACKS = ["openai/gpt-oss-120b", "llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]


# On a slow or distant link the TLS handshake is the thing that times out, and httpx drops idle
# pooled connections after 5s by default, so every voice turn pays for a fresh one. Keep the
# connection warm between turns, allow a generous handshake, and retry connect failures.
CONNECT_TIMEOUT = float(os.environ.get("BOOGEYMAN_CONNECT_TIMEOUT", "30"))
KEEPALIVE_S = float(os.environ.get("BOOGEYMAN_KEEPALIVE", "300"))
CONNECT_RETRIES = int(os.environ.get("BOOGEYMAN_CONNECT_RETRIES", "2"))


def _transport() -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(
        retries=CONNECT_RETRIES,
        limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=KEEPALIVE_S),
    )


class AllLimited(Exception):
    def __init__(self, wait: float):
        self.wait = wait

DEFAULT_MODEL = {
    "anthropic": "claude-haiku-5-5",
    "groq": "openai/gpt-oss-120b",
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "remember",
        "description": "Save one short, durable fact about the boss to long-term memory "
        "(preferences, people, projects, goals, routines). Third person, e.g. 'The boss drinks his coffee black.'",
        "parameters": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "The fact to remember, one sentence."}},
            "required": ["fact"],
        },
    },
    {
        "name": "forget",
        "description": "Remove facts from long-term memory that contain the given words (or match the fact id).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Words that identify the fact(s) to forget."}},
            "required": ["query"],
        },
    },
]


def load_text(name: str) -> str:
    try:
        # Explicit UTF-8: persona.md and boss.md carry Pidgin, accents and smart quotes, and the
        # locale default (cp1252 on Windows) either mangles them or refuses outright.
        return (HERE / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


class Replier:
    def __init__(
        self,
        provider: str,
        model: str | None,
        anthropic_key: str | None = None,
        groq_key: str | None = None,
        openai_key: str | None = None,
        openai_base: str | None = None,
        http: httpx.AsyncClient | None = None,
        memory: Memory | None = None,
        on_memory: Callable[[], Awaitable[None]] | None = None,
    ):
        self.memory = memory or Memory()
        self.on_memory = on_memory
        self.devil = False  # devil mode: colder, shorter, meaner. Same limits (see devil.md).
        # Extra tools registered by the app: name -> (schema, async handler(args) -> result str)
        self.extra: dict[str, tuple[dict[str, Any], Callable[[dict[str, Any]], Awaitable[str]]]] = {}
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=CONNECT_TIMEOUT), transport=_transport())
        self._cooldown: dict[str, float] = {}  # model -> monotonic time it can be used again
        self.provider = resolve_provider(provider, anthropic_key, groq_key, openai_key)
        self.model = model or DEFAULT_MODEL.get(self.provider, "")
        self.key = {"anthropic": anthropic_key, "groq": groq_key, "openai": openai_key}.get(self.provider)
        self.base = GROQ_URL if self.provider == "groq" else (openai_base or "").rstrip("/")
        self.anthropic = None
        if self.provider == "anthropic":
            from anthropic import AsyncAnthropic

            # The SDK keeps its own pool and retry budget; give it the same patience.
            self.anthropic = AsyncAnthropic(api_key=anthropic_key, timeout=60.0, max_retries=CONNECT_RETRIES + 1)

    @property
    def online(self) -> bool:
        return self.provider != "none"

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}" if self.online else "built-in"

    @property
    def history(self) -> list[dict[str, str]]:
        return self.memory.history[-HISTORY_TO_LLM:] if HISTORY_TO_LLM > 0 else []

    def _chain(self) -> list[str]:
        if self.provider != "groq":
            return [self.model]
        return [self.model] + [m for m in GROQ_FALLBACKS if m != self.model]

    async def _chat(self, body: dict[str, Any], chain: list[str] | None = None) -> dict[str, Any]:
        """POST a chat completion, falling back to the next free model when one is rate-limited.

        `chain` overrides the usual fallback order, which a look has to do: the free text models
        in GROQ_FALLBACKS cannot see an image and would answer about nothing.
        """
        now = time.monotonic()
        waits: list[float] = []
        for model in (chain or self._chain()):
            until = self._cooldown.get(model, 0)
            if until > now:
                waits.append(until - now)
                continue
            b = dict(body, model=model)
            b.pop("reasoning_effort", None)
            if "gpt-oss" in model:
                b["reasoning_effort"] = "low"  # keep voice replies snappy
            r = await self.http.post(f"{self.base}/chat/completions", headers={"Authorization": f"Bearer {self.key}"}, json=b)
            if r.status_code == 429 or (r.status_code in (400, 404) and model != self.model and "model" in r.text.lower()):
                wait = _retry_after(r) if r.status_code == 429 else 3600.0
                self._cooldown[model] = now + wait
                waits.append(wait)
                log.warning("%s limited (%s), resting it %.0fs; trying the next model", model, r.status_code, wait)
                continue
            r.raise_for_status()
            if model != self.model:
                log.info("answered with fallback model %s", model)
            return r.json()
        raise AllLimited(min(waits) if waits else 60.0)

    def system_parts(self) -> tuple[str, str]:
        """The system prompt in two halves: (stable, volatile).

        The stable half - the persona and the boss's own notes - is byte-identical from one turn
        to the next, so Anthropic can cache it instead of re-reading a couple of thousand tokens
        before every single answer. Everything that changes turn to turn goes in the volatile
        half, which sits after the cache breakpoint: saved facts, the clock, and the mode.
        """
        stable = [load_text("persona.md") or "You are the Boogeyman, the boss's personal AI. Speak briefly."]
        boss = load_text("boss.md")
        if boss:
            stable.append("## The boss (his own words)\n" + boss)
        facts = self.memory.facts
        volatile = [
            "## What you know about the boss\n"
            + ("\n".join(f"- {f['text']} (id {f['id']})" for f in facts) if facts else "- Nothing yet.")
        ]
        local = datetime.now().astimezone()
        volatile.append(f"## Now\n{local.strftime('%A %d %B %Y, %H:%M')} ({local.tzname()})")
        if self.devil:
            # A tone shift only: devil.md restates that every limit from the normal persona
            # still applies.
            volatile.append(load_text("devil.md") or "Devil mode: answer in one cold, flat sentence. Same limits as always.")
        volatile.append(BREVITY)  # last, because this is the one he keeps forgetting
        return "\n\n".join(stable), "\n\n".join(volatile)

    def system_prompt(self) -> str:
        stable, volatile = self.system_parts()
        return f"{stable}\n\n{volatile}"

    def system_blocks(self) -> list[dict[str, Any]]:
        """The same prompt, with a cache breakpoint after the stable half.

        The breakpoint covers the tool schemas too: they sit ahead of the system prompt in the
        cached prefix, and there are a dozen of them now that he has hands.
        """
        stable, volatile = self.system_parts()
        return [
            {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": volatile},
        ]

    def _explain(self, e: Exception) -> str:
        """What he says out loud when a brain call fails. The detail goes to the log.

        Checked most specific first: APIStatusError is an AnthropicError, and
        httpx.HTTPStatusError is an httpx.HTTPError.
        """
        if isinstance(e, AllLimited):
            mins = max(1, round(e.wait / 60))
            log.warning("every model is rate-limited; shortest wait %.0fs", e.wait)
            return (
                f"I hit the free limit on all my brains, boss. Give me about {mins} minute{'s' if mins > 1 else ''}."
                if mins < 60
                else "I've used up today's free limit, boss. It resets later today."
            )
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            log.warning("%s error %s: %s", self.provider, code, e.response.text[:300])
            if code in (401, 403):
                return f"My {self.provider} key was rejected, boss. Check it in the brain's env file."
            if code == 429:
                return "I've hit my free limit for now, boss. Give me a minute."
            return f"My brain returned an error, {code}. Try again in a moment."
        if APIStatusError and isinstance(e, APIStatusError):  # Anthropic answered with an error status
            code = e.status_code
            log.warning("anthropic error %s: %s", code, str(e)[:300])
            if code in (401, 403):
                return f"My {self.provider} key was rejected, boss. Check it in the brain's env file."
            if code == 429:
                return "I've hit my rate limit for now, boss. Give me a minute."
            return f"My brain returned an error, {code}. Try again in a moment."
        if AnthropicError and isinstance(e, AnthropicError):  # couldn't reach Anthropic at all
            log.warning("anthropic unreachable: %s", e)
            return "I can't reach my brain right now, boss. Check the internet."
        if isinstance(e, httpx.HTTPError):
            log.warning("%s unreachable: %s", self.provider, e)
            return "I can't reach my brain right now, boss. Check the internet."
        # Last resort. Anything unexpected here used to escape into the voice loop and leave him
        # silent for the turn; the traceback still goes to the terminal, but he answers.
        log.error("reply failed unexpectedly", exc_info=e)
        return "Something broke on my side, boss. Say that again."

    async def reply(self, text: str) -> str:
        if not self.online:
            return fallback(text)
        try:
            out = await (self._anthropic(text) if self.anthropic else self._openai_compatible(text))
        except Exception as e:
            return self._explain(e)
        out = out.strip() or "Done, boss."
        self.memory.add_turn(text, out)
        return out

    async def reply_stream(self, text: str) -> AsyncIterator[str]:
        """reply(), but handed over in pieces so the voice can start on the first clause.

        Waiting for the whole answer before synthesizing any of it cost about a second and a half
        of silence every turn: the brain's last token and the voice's first request were strictly
        sequential. Now they overlap.
        """
        if not self.online:
            out = fallback(text)
            yield out
            self.memory.add_turn(text, out)
            return
        out = ""
        try:
            if self.anthropic:
                async for piece in self._anthropic_stream(text):
                    out += piece
                    yield piece
            else:
                # The OpenAI-compatible path goes through _chat(), which carries the Groq
                # per-model fallback and cooldowns. Not worth forking that for streaming, so he
                # gets the answer in one piece here, exactly as he did before.
                out = await self._openai_compatible(text)
                yield out
        except Exception as e:
            msg = self._explain(e)
            if out.strip():
                # He is already mid-sentence. Tacking an error onto half an answer sounds worse
                # than just stopping, and the log keeps the detail either way.
                log.warning("reply stream broke after %d characters: %s", len(out), e)
            else:
                out = msg
                yield msg
        out = out.strip() or "Done, boss."
        self.memory.add_turn(text, out)

    # ── tools ──
    def add_tool(self, schema: dict[str, Any], handler: Callable[[dict[str, Any]], Awaitable[str]]) -> None:
        self.extra[schema["name"]] = (schema, handler)

    @property
    def tools(self) -> list[dict[str, Any]]:
        return TOOLS + [schema for schema, _ in self.extra.values()]

    async def run_tool(self, name: str, args: dict[str, Any]) -> str:
        if name in self.extra:
            try:
                result = await self.extra[name][1](args)
            except Exception as e:
                log.exception("tool %s failed", name)
                result = f"Error: {e}"
            log.info("tool %s(%s) -> %s", name, args, result[:200])
            return result
        try:
            if name == "remember":
                fact = self.memory.remember(str(args.get("fact", "")))
                result = f"Saved (id {fact['id']})."
            elif name == "forget":
                gone = self.memory.forget(str(args.get("query", "")))
                result = f"Forgot {len(gone)} fact(s)." if gone else "No matching facts."
            else:
                return f"Unknown tool {name}."
        except ValueError as e:
            return f"Error: {e}"
        log.info("tool %s(%s) -> %s", name, args, result)
        if self.on_memory:
            await self.on_memory()
        return result

    async def complete(self, system: str, prompt: str) -> str:
        """One plain completion, no tools, no history (used by the research agent)."""
        if self.anthropic:
            msg = await self.anthropic.messages.create(
                model=self.model, max_tokens=900, system=system, messages=[{"role": "user", "content": prompt}]
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "max_tokens": 900,
            "temperature": 0.3,
        }
        data = await self._chat(body)
        return str(data["choices"][0]["message"].get("content") or "")

    @property
    def can_see(self) -> bool:
        """Is there a model behind this provider that can take an image?"""
        return bool(self.anthropic) or self.provider in ("groq", "openai")

    async def look(self, system: str, prompt: str, img: bytes, media: str = "image/jpeg") -> str:
        """One look at an image: no tools, no history, no memory of it afterwards.

        Deliberately stateless. A screen is read dozens of times in a study session and none of
        those frames belong in the conversation the boss can hear played back to him.
        """
        b64 = base64.b64encode(img).decode()
        if self.anthropic:
            msg = await self.anthropic.messages.create(
                model=self.model,
                max_tokens=VISION_TOKENS,
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                **NO_THINKING,
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        model = GROQ_VISION if self.provider == "groq" else self.model
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}},
                    ],
                },
            ],
            "max_tokens": VISION_TOKENS,
            "temperature": 0.2,
        }
        data = await self._chat(body, chain=[model])
        return str(data["choices"][0]["message"].get("content") or "").strip()

    # ── OpenAI-compatible (Groq, Gemini, OpenRouter...) ──
    async def _openai_compatible(self, text: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt()},
            *self.history,
            {"role": "user", "content": text},
        ]
        tools = [{"type": "function", "function": t} for t in self.tools]
        for _ in range(MAX_TOOL_ROUNDS):
            body: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_tokens": VOICE_TOKENS,
                "temperature": 0.7,
                "tools": tools,
                "tool_choice": "auto",
            }
            msg = (await self._chat(body))["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls:
                return str(msg.get("content") or "")
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {})
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await self.run_tool(fn.get("name", ""), args)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
        return "Done, boss."

    # ── Anthropic ──
    async def _anthropic(self, text: str) -> str:
        messages: list[dict[str, Any]] = [*self.history, {"role": "user", "content": text}]
        tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in self.tools]
        # Build the system prompt once and reuse it for every round. It carries the current time
        # down to the minute, and the signature on a thinking block is bound to the conversation
        # prefix: if a tool runs across a minute boundary, a rebuilt prompt invalidates it and the
        # whole turn comes back as a 400.
        system = self.system_blocks()
        for _ in range(MAX_TOOL_ROUNDS):
            msg = await self.anthropic.messages.create(  # type: ignore[union-attr]
                model=self.model, max_tokens=VOICE_TOKENS, system=system, messages=messages, tools=tools, **NO_THINKING
            )
            uses = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
            if not uses:
                return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            messages.append({"role": "assistant", "content": [b.model_dump() for b in msg.content]})
            results = []
            for u in uses:
                results.append(
                    {"type": "tool_result", "tool_use_id": u.id, "content": await self.run_tool(u.name, dict(u.input))}
                )
            messages.append({"role": "user", "content": results})
        return "Done, boss."

    async def _anthropic_stream(self, text: str) -> AsyncIterator[str]:
        """_anthropic(), yielding text as it is generated.

        Text that arrives before a tool call is yielded too, rather than held back: hearing
        "let me check that" while a search runs beats hearing nothing at all.
        """
        messages: list[dict[str, Any]] = [*self.history, {"role": "user", "content": text}]
        tools = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in self.tools]
        system = self.system_blocks()  # once per turn, for the reason given in _anthropic()
        for _ in range(MAX_TOOL_ROUNDS):
            async with self.anthropic.messages.stream(  # type: ignore[union-attr]
                model=self.model, max_tokens=VOICE_TOKENS, system=system, messages=messages, tools=tools, **NO_THINKING
            ) as stream:
                async for ev in stream:
                    if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                        yield ev.delta.text
                msg = await stream.get_final_message()
            uses = [b for b in msg.content if getattr(b, "type", "") == "tool_use"]
            if not uses:
                return
            messages.append({"role": "assistant", "content": [b.model_dump() for b in msg.content]})
            results = []
            for u in uses:
                results.append(
                    {"type": "tool_result", "tool_use_id": u.id, "content": await self.run_tool(u.name, dict(u.input))}
                )
            messages.append({"role": "user", "content": results})


def resolve_provider(provider: str, anthropic_key: str | None, groq_key: str | None, openai_key: str | None) -> str:
    provider = (provider or "auto").lower()
    have = {"anthropic": bool(anthropic_key), "groq": bool(groq_key), "openai": bool(openai_key)}
    if provider == "auto":
        for p in ("anthropic", "groq", "openai"):
            if have[p]:
                return p
        return "none"
    if provider in have and not have[provider]:
        log.warning("LLM_PROVIDER=%s but its key is missing; using built-in replies", provider)
        return "none"
    return provider if provider in have else "none"


def _retry_after(r: httpx.Response) -> float:
    """How long Groq says to wait: Retry-After header, else 'try again in 1m2.5s' in the body."""
    h = r.headers.get("retry-after")
    if h:
        try:
            return max(1.0, float(h))
        except ValueError:
            pass
    import re

    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", r.text)
    if m and any(m.groups()):
        h_, m_, s_ = m.groups()
        return max(1.0, int(h_ or 0) * 3600 + int(m_ or 0) * 60 + float(s_ or 0))
    return 60.0


def fallback(text: str) -> str:
    t = text.lower().strip(" .!?")
    if any(w in t for w in ("hello", "hi", "hey", "yo", "good morning", "good evening")):
        return random.choice(FALLBACK_GREETINGS)
    if "how are you" in t:
        return "Running clean, boss. All systems up."
    if "your name" in t or "who are you" in t:
        return "I'm the Boogeyman. Your personal intelligence."
    return f"I heard you say: {text}. Give me a brain key and I'll do more than repeat you."
