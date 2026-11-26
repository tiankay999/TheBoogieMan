"""The Boogeyman's brain.

Spawned by the Rust core on app start. Rust tells us which port to use via the
BOOGEYMAN_BRAIN_PORT env var and connects to ws://127.0.0.1:<port>/ws.

Every message, in both directions, has the same shape (see shared/messages.md):
    {"id": "<uuid>", "type": "<kind>", "payload": {...}}
A reply reuses the id of the request it answers. Events the brain pushes on its
own (voice state, audio levels, captions) have an empty id.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import config
import eyes
import hands
from commands import Commands
from memory import Memory
from reply import Replier
from research import Research
from study import Study
from study import wanted as study_wanted
from voice import VoiceLoop

log = logging.getLogger("brain")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)


def make_message(msg_id: str, msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": msg_id, "type": msg_type, "payload": payload}


class Hub:
    """Connected UI sockets. Events are broadcast to all of them."""

    def __init__(self) -> None:
        self.clients: dict[WebSocket, asyncio.Lock] = {}

    async def send(self, ws: WebSocket, msg: dict[str, Any]) -> None:
        lock = self.clients.get(ws)
        if lock is None:
            return
        async with lock:
            try:
                await ws.send_json(msg)
            except Exception:
                self.clients.pop(ws, None)

    async def emit(self, msg_type: str, payload: dict[str, Any]) -> None:
        for ws in list(self.clients):
            await self.send(ws, make_message("", msg_type, payload))


hub = Hub()
settings = config.load()
memory = Memory()


async def push_memory() -> None:
    await hub.emit("memory.updated", memory.snapshot())


replier = Replier(
    settings.llm_provider,
    settings.model,
    anthropic_key=settings.anthropic_key,
    groq_key=settings.groq_key,
    openai_key=settings.openai_key,
    openai_base=settings.openai_base,
    memory=memory,
    on_memory=push_memory,
)
commands = Commands()
state: dict[str, Any] = {"greeted": False, "capabilities": {}, "action": None, "devil": False, "tts": None, "edge": None, "ears": None}
# Study mode. `voice` is built further down (it needs think(), which needs this), so the two
# hooks into it are lambdas: both are resolved when he actually speaks, by which time it exists.
study = Study(
    replier.look,
    lambda t: voice.say(t),
    hub.emit,
    quiet=lambda: not voice.speaking,
)
ORPHEUS_NOTE = (
    "Orpheus can't be used with this key, so the Edge voice is being used instead. That is no "
    "loss: measured head to head, Orpheus starts about 600ms sooner but then talks 1.43s "
    "longer, and its speed parameter is ignored, so Edge wins by roughly 800ms a reply."
)


async def set_devil(on: bool) -> None:
    """Devil mode is not just a colour scheme: it changes how he thinks and how he sounds."""
    state["devil"] = on
    replier.devil = on  # colder, shorter persona (brain/devil.md); same limits
    if state["tts"] is not None:
        state["tts"].devil = on  # slower, less stable voice
    await hub.emit("ui.devil", {"on": on})
    log.info("devil mode %s", "on" if on else "off")


RESEARCH_CMD = re.compile(r"^(?:please\s+)?(?:research|look\s+up|search\s+(?:for|the\s+web\s+for)|find\s+out\s+(?:about)?|google)\s+(.+)", re.I)


async def ui_action(action: str | None) -> str | None:
    """Apply UI actions right away; return anything that must wait until after speaking."""
    if action in ("work", "normal"):
        await hub.emit("ui.mode", {"mode": action})
        return None
    if action and action.startswith("devil:"):
        await set_devil(action == "devil:on")
        return None
    if action == "help":
        await hub.emit("ui.help", {"show": True})
        return None
    if action == "clear":
        memory.clear_history()
        await push_memory()
        return None
    if action and action.startswith("bg:"):
        await hub.emit("ui.background", {"cmd": action[3:]})
        return None
    return action


async def run_research(query: str) -> str:
    await hub.emit("ui.mode", {"mode": "work"})
    agent = Research(hub.emit, replier.complete if replier.online else None)
    result = await agent.run(query)
    lines = [f"Summary: {result['summary']}"] + [f"- {f}" for f in result["findings"]]
    return "\n".join(lines)


async def study_command(text: str) -> str | None:
    """Study mode's own triggers, which run ahead of everything else.

    Each returns None when it isn't really a command for this mode, so the sentence falls
    through to the normal path: "stop studying" with nothing running, or "explain that" when
    he has never looked at a screen, are both just things the boss said.
    """
    want = study_wanted(text)
    if want is None:
        return None
    if want == "on":
        return await study.start()
    if want == "off":
        return await study.stop() if study.on else None
    return await study.further() if study.last_shot else None


async def think(text: str) -> str:
    """Built-in commands first (instant, offline), then the LLM."""
    said = await study_command(text)
    if said is not None:
        return said
    hit = commands.handle(text)
    if hit:
        out, action = hit
        state["action"] = await ui_action(action)
        log.info("command: %r -> %s", text, action or "reply")
        return out
    m = RESEARCH_CMD.match(text.strip())
    if m:  # "research X" goes straight to the agent: faster, and works without tool-calling
        await hub.emit("voice.state", {"state": "thinking"})
        result = await run_research(m.group(1).strip(" .?!"))
        return result.split("\n", 1)[0].removeprefix("Summary: ")
    return await replier.reply(text)


async def think_stream(text: str) -> AsyncIterator[str]:
    """think(), handed over in pieces so the voice can start before the brain has finished.

    The first two tiers answer instantly and have nothing to stream, so they yield once. Only
    the LLM tier benefits, which is also the only one that was ever slow.
    """
    said = await study_command(text)
    if said is not None:
        yield said
        return
    hit = commands.handle(text)
    if hit:
        out, action = hit
        state["action"] = await ui_action(action)
        log.info("command: %r -> %s", text, action or "reply")
        yield out
        return
    m = RESEARCH_CMD.match(text.strip())
    if m:
        await hub.emit("voice.state", {"state": "thinking"})
        result = await run_research(m.group(1).strip(" .?!"))
        yield result.split("\n", 1)[0].removeprefix("Summary: ")
        return
    async for part in replier.reply_stream(text):
        yield part


async def tool_shutdown(args: dict[str, Any]) -> str:
    if args.get("now"):
        state["action"] = "shutdown"
        return "Shutdown scheduled: the app closes right after your reply. Say a very short goodbye."
    return f"Not shut down yet. Ask exactly this and nothing else: {commands.ask_shutdown()}"


async def tool_switch_screen(args: dict[str, Any]) -> str:
    screen = str(args.get("screen", "")).lower()
    if screen not in ("work", "normal"):
        return "Error: screen must be 'work' or 'normal'."
    await hub.emit("ui.mode", {"mode": screen})
    return f"Switched to the {screen} screen."


async def tool_devil_mode(args: dict[str, Any]) -> str:
    on = bool(args.get("on", True))
    await set_devil(on)
    return (
        "Devil mode is on: visor down, red, the eye is open. Answer from here on exactly as devil mode tells you to."
        if on
        else "Devil mode is off. Back to your normal voice."
    )


async def tool_set_background(args: dict[str, Any]) -> str:
    name = str(args.get("background", "")).lower()
    if name not in ("next", "sea", "nebula", "custom", "off", "on"):
        return "Error: background must be one of next, sea, nebula, custom, off, on."
    await hub.emit("ui.background", {"cmd": name})
    return f"Background: {name}."


async def tool_show_commands(args: dict[str, Any]) -> str:
    await hub.emit("ui.help", {"show": bool(args.get("show", True))})
    return "The commands card is on his screen. Just say one line about it."


async def tool_clear_conversation(args: dict[str, Any]) -> str:
    memory.clear_history()
    await push_memory()
    return "Recent conversation cleared (long-term facts kept)."


async def _hands(fn, *a: Any) -> str:
    """Run a blocking hands call off the event loop, turning refusals into plain speech."""
    try:
        return await asyncio.to_thread(fn, *a)
    except hands.Unsafe as e:
        return f"Refused: {e}"


async def tool_open_app(args: dict[str, Any]) -> str:
    return await _hands(hands.open_app, str(args.get("app", "")))


async def tool_close_app(args: dict[str, Any]) -> str:
    return await _hands(hands.close_app, str(args.get("app", "")))


async def tool_list_files(args: dict[str, Any]) -> str:
    return await _hands(hands.list_files, str(args.get("folder", "")))


async def tool_create_file(args: dict[str, Any]) -> str:
    return await _hands(
        hands.create_file, str(args.get("name", "")), str(args.get("content", "")), bool(args.get("overwrite", False))
    )


async def tool_delete_file(args: dict[str, Any]) -> str:
    return await _hands(hands.delete_file, str(args.get("name", "")), bool(args.get("confirmed", False)))


async def tool_type_text(args: dict[str, Any]) -> str:
    return await _hands(
        hands.type_text, str(args.get("text", "")), bool(args.get("confirmed", False)), bool(args.get("press_enter", False))
    )


async def tool_focused_window(args: dict[str, Any]) -> str:
    where = await asyncio.to_thread(hands.focused_window)
    return f"The focused window is {where!r}." if where else "I can't tell which window is focused."


async def tool_research(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: empty query."
    return await run_research(query)


replier.add_tool(
    {
        "name": "shutdown",
        "description": "Shut yourself down: closes the Boogeyman app completely (not the computer). "
        "You CAN do this. Use it whenever the boss wants you off, gone, asleep, closed or offline, however he says it. "
        "now=true when he says now/immediately or has already confirmed; now=false asks him to confirm first.",
        "parameters": {
            "type": "object",
            "properties": {"now": {"type": "boolean", "description": "true = close right after this reply; false = ask to confirm"}},
            "required": ["now"],
        },
    },
    tool_shutdown,
)
replier.add_tool(
    {
        "name": "switch_screen",
        "description": "Switch the boss's screen. 'work' opens the workspace (for tasks and research); "
        "'normal' returns to the main screen with the core. Use for 'let's work', 'back to normal', 'main screen', "
        "'leave the workspace', and similar.",
        "parameters": {
            "type": "object",
            "properties": {"screen": {"type": "string", "enum": ["work", "normal"]}},
            "required": ["screen"],
        },
    },
    tool_switch_screen,
)
replier.add_tool(
    {
        "name": "devil_mode",
        "description": "Turn devil mode on or off. Devil mode is the serious, armed look: the helmet visor drops, "
        "everything goes red and your eye opens. Use for 'devil mode', 'go devil', 'unleash', 'get serious', "
        "and on=false for 'stand down', 'exit devil mode'.",
        "parameters": {"type": "object", "properties": {"on": {"type": "boolean"}}, "required": ["on"]},
    },
    tool_devil_mode,
)
replier.add_tool(
    {
        "name": "set_background",
        "description": "Change the moving background video. 'sea' = underwater shipwreck, 'nebula' = dark space smoke, "
        "'custom' = his own clip, 'next' = cycle, 'off' / 'on' = hide / show.",
        "parameters": {
            "type": "object",
            "properties": {"background": {"type": "string", "enum": ["next", "sea", "nebula", "custom", "off", "on"]}},
            "required": ["background"],
        },
    },
    tool_set_background,
)
replier.add_tool(
    {
        "name": "show_commands",
        "description": "Show (or hide) the card listing everything the boss can say and do. "
        "Use when he asks what you can do, for help, or for the commands.",
        "parameters": {"type": "object", "properties": {"show": {"type": "boolean"}}, "required": []},
    },
    tool_show_commands,
)
replier.add_tool(
    {
        "name": "clear_conversation",
        "description": "Wipe the recent conversation history (long-term memory facts stay). "
        "Use when he asks to clear or reset the chat, or wants a fresh start.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    tool_clear_conversation,
)
replier.add_tool(
    {
        "name": "open_app",
        "description": "Open an application, or a file in his workspace. You CAN do this. "
        "Use it for 'open the browser', 'launch spotify', 'start notepad', 'open my notes'. "
        "Give the app the way he said it; common names are mapped for you.",
        "parameters": {
            "type": "object",
            "properties": {"app": {"type": "string", "description": "App or file name, in plain words."}},
            "required": ["app"],
        },
    },
    tool_open_app,
)
replier.add_tool(
    {
        "name": "close_app",
        "description": "Close a running application. You CAN do this. Use it for 'close spotify', "
        "'quit the browser', 'shut chrome down'. It refuses system-critical processes. "
        "To close YOURSELF, use the shutdown tool instead, never this one.",
        "parameters": {
            "type": "object",
            "properties": {"app": {"type": "string", "description": "App name, in plain words."}},
            "required": ["app"],
        },
    },
    tool_close_app,
)
replier.add_tool(
    {
        "name": "list_files",
        "description": "List what's in the boss's workspace folder. Use it before deleting anything, "
        "and whenever he asks what files he has or what's in a folder.",
        "parameters": {
            "type": "object",
            "properties": {"folder": {"type": "string", "description": "Sub-folder, or empty for the workspace root."}},
            "required": [],
        },
    },
    tool_list_files,
)
replier.add_tool(
    {
        "name": "create_file",
        "description": "Write a text file into the boss's workspace folder. You CAN do this. "
        "Use it for 'save that as a file', 'write me a note', 'put that in a text file'. "
        "Only set overwrite=true after he has agreed to replace an existing file.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "File name, e.g. 'notes/ideas.txt'."},
                "content": {"type": "string", "description": "What goes in the file."},
                "overwrite": {"type": "boolean", "description": "Replace an existing file (ask him first)."},
            },
            "required": ["name", "content"],
        },
    },
    tool_create_file,
)
replier.add_tool(
    {
        "name": "delete_file",
        "description": "Delete a file from the boss's workspace, into a trash folder so it can come back. "
        "ALWAYS call it first with confirmed=false: that arms it and gives you the exact question to ask him. "
        "Only call again with confirmed=true after he has actually said yes out loud. Never guess a file name: "
        "use list_files if you aren't certain what he means.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "File name as it appears in the workspace."},
                "confirmed": {"type": "boolean", "description": "true only after he said yes to the question."},
            },
            "required": ["name", "confirmed"],
        },
    },
    tool_delete_file,
)
replier.add_tool(
    {
        "name": "focused_window",
        "description": "Name the window that currently has focus, i.e. where typing would land. "
        "Use it when he asks what's open or in front, and before typing if you're unsure.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    tool_focused_window,
)
replier.add_tool(
    {
        "name": "type_text",
        "description": "Type text on the keyboard into whatever window the boss has focused: his browser, "
        "a search box, a document, a chat. You CAN do this. Use it for 'type this in my browser', "
        "'search for X', 'write that in the box'. "
        "ALWAYS call it first with confirmed=false: that tells you which window is focused and gives you "
        "the exact question to ask. Only call again with confirmed=true after he has actually said go ahead. "
        "Set press_enter=true only if he wants it sent or searched.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Exactly what to type."},
                "confirmed": {"type": "boolean", "description": "true only after he said go ahead."},
                "press_enter": {"type": "boolean", "description": "Press Enter afterwards (sends/searches)."},
            },
            "required": ["text", "confirmed"],
        },
    },
    tool_type_text,
)
async def tool_look(args: dict[str, Any]) -> str:
    if not eyes.HAVE_EYES:
        return "Error: screen capture isn't installed. Run: pip install mss pillow"
    if not replier.can_see:
        return "Error: this brain has no model that can look at an image."
    try:
        shot = await eyes.grab()
    except eyes.Blind as e:
        return f"Error: {e}"
    question = str(args.get("question") or "").strip() or "What is on this screen?"
    return await replier.look(
        "You are looking at the boss's screen and telling him what is there. He cannot see it "
        "through you, so be specific: name what you actually read. Two or three sentences.",
        question,
        shot,
        eyes.MEDIA,
    )


replier.add_tool(
    {
        "name": "look_at_screen",
        "description": "Look at the boss's screen and answer a question about what is on it. Use this "
        "whenever he asks what something says, what he is looking at, what an error means, or why "
        "something on screen is wrong. Prefer it over guessing.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What to find out from the screen, e.g. 'what does this error say?'",
                }
            },
            "required": [],
        },
    },
    tool_look,
)


async def tool_study(args: dict[str, Any]) -> str:
    on = args.get("on")
    if on is None:
        on = True
    return await study.start() if on else await study.stop()


replier.add_tool(
    {
        "name": "study_mode",
        "description": "Turn study mode on or off. While it is on he watches the boss's screen and, "
        "each time a question appears, says the answer and one short reason, then does the same for "
        "the next question. Use it whenever he asks you to watch his screen, help him study or revise, "
        "or go through questions with him. This exists: never tell him there is no study mode.",
        "parameters": {
            "type": "object",
            "properties": {
                "on": {"type": "boolean", "description": "true to start watching, false to stop."}
            },
            "required": ["on"],
        },
    },
    tool_study,
)


replier.add_tool(
    {
        "name": "research",
        "description": "Search the web and read sources to answer something current or factual "
        "(news, prices, events, people, anything to look up). Shows the work live on the boss's screen. "
        "Returns a summary and findings with [n] citations.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to research, in plain words."}},
            "required": ["query"],
        },
    },
    tool_research,
)


async def after_turn() -> None:
    """Runs after a reply has been spoken."""
    action, state["action"] = state["action"], None
    if action == "shutdown":
        await hub.emit("app.shutdown", {})


voice = VoiceLoop(hub.emit, None, None, None, None, think, after_turn=after_turn, reply_stream=think_stream)
tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    tasks.add(t)
    t.add_done_callback(tasks.discard)


def setup_voice() -> dict[str, Any]:
    """Pick ears and a voice from what's available, and degrade instead of going quiet.

    ElevenLabs is used when its key is set, but it runs out of credits and keys arrive with the
    wrong permissions, so `auto` wraps it: the first time it refuses for good, he moves to the
    free stack (Edge for the voice, Groq Whisper for the ears) and stays there for the session.
    """
    caps: dict[str, Any] = {
        "brain": replier.label, "claude": replier.online, "elevenlabs": False,
        "tts": None, "stt": None, "mic": False, "speaker": False, "notes": [],
        "eyes": eyes.HAVE_EYES and replier.can_see,
    }
    if not settings.voice_enabled:
        caps["notes"].append("voice disabled (BOOGEYMAN_VOICE=0)")
        return caps

    from freevoice import TTS_RATE, EdgeTTS, FallbackSTT, FallbackTTS, GroqSTT, GroqTTS

    want_tts, want_stt = settings.tts_provider, settings.stt_provider
    el = None
    if settings.elevenlabs_key and (want_tts in ("auto", "elevenlabs") or want_stt in ("auto", "elevenlabs")):
        from elevenlabs import ElevenLabs

        el = ElevenLabs(
            settings.elevenlabs_key,
            settings.voice_id,
            settings.tts_model,
            settings.stt_model,
            devil_voice_id=settings.devil_voice_id,
        )
        caps["elevenlabs"] = True

    def note(why: str) -> None:
        caps["notes"].append(f"switched to the free voice stack: {why[:90]}")

    # ── his voice ──
    # Edge is the free default. Orpheus was tried as the quicker option and measured slower
    # overall - it starts ~600ms sooner but talks 1.43s longer on the same line, and its speed
    # parameter is silently ignored, so that cannot be tuned out. See GroqTTS for the numbers.
    # It stays available for anyone who prefers the voice: BOOGEYMAN_TTS=groq.
    edge = EdgeTTS()
    free: Any = GroqTTS(settings.groq_key) if (settings.groq_key and want_tts == "groq") else edge
    if want_tts == "elevenlabs" and el:
        tts: Any = el
    elif want_tts in ("edge", "groq") or el is None:
        tts = edge if want_tts == "edge" else free
        if want_tts == "auto":
            caps["notes"].append("no ELEVENLABS_API_KEY: using the free voice")
    else:
        tts = FallbackTTS(el, free, on_switch=note)
    tts.devil = state["devil"]
    state["tts"] = tts  # so set_devil() can darken whichever voice is live
    state["edge"] = edge  # the standby, if Orpheus turns out to be locked
    voice.tts = tts.speak
    voice.tts_obj = tts  # lets him synthesize a clause at a time, and talk while still thinking
    caps["tts"] = getattr(tts, "label", "elevenlabs")

    # ── his ears ──
    groq_stt = GroqSTT(settings.groq_key) if settings.groq_key else None
    if want_stt == "elevenlabs" and el:
        voice.transcribe, caps["stt"] = el.transcribe, "elevenlabs"
    elif want_stt == "groq" and groq_stt:
        voice.transcribe, caps["stt"] = groq_stt.transcribe, f"groq:{groq_stt.model}"
    elif el and groq_stt:
        ears = FallbackSTT(el, groq_stt, on_switch=note)
        state["ears"] = ears  # settle_voice() promotes the backup if the key turns out to be dead
        voice.transcribe, caps["stt"] = ears.transcribe, "elevenlabs (free fallback ready)"
    elif el:
        voice.transcribe, caps["stt"] = el.transcribe, "elevenlabs"
    elif groq_stt:
        voice.transcribe, caps["stt"] = groq_stt.transcribe, f"groq:{groq_stt.model}"
    else:
        caps["notes"].append("no speech-to-text: set GROQ_API_KEY or ELEVENLABS_API_KEY")

    try:
        from audio import Speaker

        voice.speaker = Speaker(TTS_RATE)
        caps["speaker"] = True
    except Exception as e:
        caps["notes"].append(f"no speaker: {e}")
    log.info("voice: %s | ears: %s", caps["tts"], caps["stt"])
    try:
        from audio import Mic

        voice.mic = Mic(settings.mic_device)
        caps["mic"] = True
    except Exception as e:
        caps["notes"].append(f"no microphone: {e}")
    if not replier.online:
        caps["notes"].append("no LLM key (GROQ_API_KEY / ANTHROPIC_API_KEY): using built-in replies")
    else:
        log.info("brain: %s", replier.label)
    return caps


async def settle_voice() -> None:
    """Work out which voices actually function, before the first turn rather than during it.

    Two answers are needed, and both cost a round trip to discover the hard way:

      - Orpheus is only usable once the model's terms are accepted in the Groq console. Until
        then every request is a 400. It may be sitting there as the *backup* voice, and a backup
        that doesn't work is worse than no backup at all - he just goes silent.
      - ElevenLabs runs out of credits. Finding that out mid-sentence means the first turn of
        every session is spent discovering it, and the live streaming path can't be used until
        the switch has happened.

    Settling both here means the voice he starts the session with is one that works.
    """
    caps, tts, edge = state["capabilities"], state["tts"], state["edge"]
    if tts is None:
        return

    checked: dict[int, bool] = {}

    async def works(v: Any) -> bool:
        """Can this provider actually be used? Asked once per object, then remembered."""
        if not hasattr(v, "available"):
            return True
        if id(v) not in checked:
            checked[id(v)] = await v.available()
        return checked[id(v)]

    if hasattr(tts, "backup"):  # FallbackTTS: both ends have to be sound
        if not await works(tts.backup):
            caps["notes"].append(ORPHEUS_NOTE)
            tts.backup = edge
        if not await works(tts.primary):
            # Dead primary: promote the backup now and skip the wasted first attempt.
            caps["notes"].append("ElevenLabs can't be used: switched to the free voice.")
            state["tts"] = tts = tts.backup
            voice.tts, voice.tts_obj = tts.speak, tts
    elif not await works(tts):
        caps["notes"].append(ORPHEUS_NOTE)
        state["tts"] = tts = edge
        voice.tts, voice.tts_obj = edge.speak, edge

    tts.devil = state["devil"]
    caps["tts"] = getattr(tts, "label", caps["tts"])

    # The ears get the same treatment: no point spending the first thing the boss says on
    # discovering the key is dead. `works` has already asked, so this costs nothing.
    ears = state.get("ears")
    if ears is not None and not await works(ears.primary):
        voice.transcribe = ears.backup.transcribe
        caps["stt"] = f"groq:{getattr(ears.backup, 'model', 'whisper')}"

    log.info("voice ready: %s | ears: %s", caps["tts"], caps["stt"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    state["capabilities"] = setup_voice()
    await settle_voice()
    for note in state["capabilities"]["notes"]:
        log.warning(note)
    if not state["capabilities"].get("eyes"):
        why = "mss/pillow aren't installed" if not eyes.HAVE_EYES else "this brain can't see images"
        log.warning("no eyes: %s, so study mode and look_at_screen are off", why)
    spawn(voice.run())
    yield
    await study.stop()
    for t in list(tasks):
        t.cancel()


app = FastAPI(title="Boogeyman brain", lifespan=lifespan)


async def handle(message: dict[str, Any]) -> dict[str, Any]:
    """Turn one incoming request into one reply."""
    msg_id = str(message.get("id", ""))
    msg_type = message.get("type")
    payload = message.get("payload") or {}

    if msg_type == "ping":
        return make_message(msg_id, "pong", {})

    if msg_type == "user.message":
        text = str(payload.get("text", "")).strip()
        out = await think(text) if text else "Welcome, boss. I'm listening."

        async def speak_then_act() -> None:
            await voice.say(out)
            await after_turn()

        spawn(speak_then_act())
        return make_message(
            msg_id,
            "brain.reply",
            {
                "text": out,
                "chars": len(out),
                "python": platform.python_version(),
                "at": datetime.now(timezone.utc).isoformat(),
            },
        )

    if msg_type == "voice.greet":
        first = not state["greeted"]
        state["greeted"] = True
        if first:
            greeting = "Welcome back, boss. I'm listening."
            await hub.emit("voice.reply", {"text": greeting})
            spawn(voice.say(greeting))
        return make_message(
            msg_id, "voice.ack", {"spoken": first, "capabilities": state["capabilities"], "memory": memory.snapshot()}
        )

    if msg_type == "voice.mute":
        await voice.set_muted(bool(payload.get("muted")))
        return make_message(msg_id, "voice.ack", {"muted": voice.muted})

    if msg_type == "memory.get":
        return make_message(msg_id, "memory.updated", memory.snapshot())

    if msg_type == "memory.forget":
        memory.forget(str(payload.get("id", "")))
        await push_memory()
        return make_message(msg_id, "memory.updated", memory.snapshot())

    if msg_type == "voice.status":
        return make_message(msg_id, "voice.ack", {"muted": voice.muted, "capabilities": state["capabilities"]})

    return make_message(msg_id, "error", {"message": f"Unknown message type: {msg_type!r}"})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "python": platform.python_version(), "pid": os.getpid(), "voice": state["capabilities"]}


@app.websocket("/ws")
async def ws(socket: WebSocket) -> None:
    await socket.accept()
    hub.clients[socket] = asyncio.Lock()

    async def answer(message: dict[str, Any]) -> None:
        await hub.send(socket, await handle(message))

    try:
        while True:
            try:
                message = await socket.receive_json()
            except ValueError:
                await hub.send(socket, make_message("", "error", {"message": "Message was not valid JSON"}))
                continue
            if not isinstance(message, dict):
                await hub.send(socket, make_message("", "error", {"message": "Message must be a JSON object"}))
                continue
            # Handle concurrently so a slow request never blocks voice events or other requests.
            spawn(answer(message))
    except WebSocketDisconnect:
        pass
    finally:
        hub.clients.pop(socket, None)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("BOOGEYMAN_BRAIN_PORT", "8765"))
    print(f"[brain] Python {platform.python_version()} listening on 127.0.0.1:{port}", file=sys.stderr, flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
