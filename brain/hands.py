"""The Boogeyman's hands: opening and closing apps, and creating and deleting files.

He hears you through a microphone and decides with a language model, so both of those can be
wrong. Three rules keep a mis-heard sentence from doing real damage:

  1. File work is confined to one workspace folder (BOOGEYMAN_WORKSPACE, default ~/Boogeyman).
     Anything that resolves outside it is refused, symlinks included.
  2. Deleting is two-step and the gate is enforced here, not in the prompt: the first call only
     arms it and hands back the question to ask. The real delete needs a second call for the
     same path within ARM_WINDOW seconds, and moves the file into .trash instead of destroying
     it, so "undo" is always possible.
  3. Closing an app refuses anything protected: the shell, the desktop, and the Boogeyman itself.

Opening never goes through a shell, so nothing the model says can be parsed as shell syntax.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.hands")

WINDOWS = platform.system() == "Windows"
ARM_WINDOW = 60.0  # seconds an armed deletion stays valid
MAX_WRITE = 200_000  # characters accepted in one create_file
MAX_LIST = 60  # entries returned by list_files

# Spoken names mapped to what to actually launch. Anything else is tried as a command name.
ALIASES: dict[str, dict[str, str]] = {
    "windows": {
        "browser": "msedge", "edge": "msedge", "chrome": "chrome", "firefox": "firefox",
        "brave": "brave", "notepad": "notepad", "notes": "notepad", "calculator": "calc",
        "calc": "calc", "explorer": "explorer", "files": "explorer", "file explorer": "explorer",
        "terminal": "wt", "command prompt": "cmd", "powershell": "powershell", "paint": "mspaint",
        "task manager": "taskmgr", "spotify": "spotify", "vs code": "code", "vscode": "code",
        "code": "code", "word": "winword", "excel": "excel",
    },
    "linux": {
        "browser": "xdg-open", "firefox": "firefox", "chrome": "google-chrome",
        "brave": "brave-browser", "files": "nautilus", "file manager": "nautilus",
        "terminal": "gnome-terminal", "calculator": "gnome-calculator", "text editor": "gedit",
        "vs code": "code", "vscode": "code", "code": "code", "spotify": "spotify",
    },
}

# Never close these: it would take the desktop, or us, down with them.
PROTECTED = {
    "explorer.exe", "winlogon.exe", "csrss.exe", "services.exe", "lsass.exe", "smss.exe",
    "wininit.exe", "svchost.exe", "dwm.exe", "taskmgr.exe", "system", "registry", "memory compression",
    "boogeyman.exe", "python.exe", "pythonw.exe", "node.exe",
    "systemd", "init", "gnome-shell", "xorg", "wayland", "pulseaudio", "pipewire",
    "boogeyman", "python3", "node",
}

# Compare without the extension, so "explorer" is as protected as "explorer.exe".
PROTECTED_STEMS = {Path(n).stem.lower() for n in PROTECTED}

# Characters that only make sense if someone is trying to smuggle in a second command.
SHELL_CHARS = set(';|&$`\n\r><')


class Unsafe(Exception):
    """Refused: outside the workspace, protected, or malformed."""


def aliases() -> dict[str, str]:
    return ALIASES["windows" if WINDOWS else "linux"]


# ── the workspace ──
def workspace() -> Path:
    base = os.environ.get("BOOGEYMAN_WORKSPACE") or (Path.home() / "Boogeyman")
    ws = Path(base).expanduser()
    ws.mkdir(parents=True, exist_ok=True)
    return ws.resolve()


def trash_dir() -> Path:
    t = workspace() / ".trash"
    t.mkdir(parents=True, exist_ok=True)
    return t


def safe_path(name: str) -> Path:
    """Resolve `name` inside the workspace. Raises Unsafe if it escapes, symlinks included."""
    raw = str(name).strip().strip('"').strip("'")
    if not raw:
        raise Unsafe("you didn't say which file")
    ws = workspace()
    p = Path(os.path.expandvars(raw)).expanduser()
    if not p.is_absolute():
        p = ws / p
    # Resolve the deepest part that already exists, so a symlinked parent can't point outside.
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    base = probe.resolve()
    if base != ws and ws not in base.parents:
        raise Unsafe(f"that's outside your workspace ({ws})")
    final = base if p == probe else base / p.relative_to(probe)
    resolved = final.resolve()
    if resolved != ws and ws not in resolved.parents:
        raise Unsafe(f"that's outside your workspace ({ws})")
    return final


def pretty(p: Path) -> str:
    try:
        return str(p.relative_to(workspace()))
    except ValueError:
        return str(p)


# ── opening apps ──
def resolve_command(name: str) -> list[str]:
    """Turn a spoken app name into argv. Never involves a shell."""
    want = " ".join(str(name).split()).strip().strip('"').strip("'")
    if not want:
        raise Unsafe("you didn't say which app")
    if any(c in SHELL_CHARS for c in want):
        raise Unsafe("that app name has characters I won't run")
    target = aliases().get(want.lower(), want)
    found = shutil.which(target)
    if found:
        return [found]
    # Not on PATH. It may still be a document in the workspace, or a shell-registered app.
    try:
        p = safe_path(want)
        if p.exists():
            return [str(p)]
    except Unsafe:
        pass
    if WINDOWS:
        return [target]  # let ShellExecute try it (ms-settings:, store apps, ...)
    raise Unsafe(f"I couldn't find an app called {want!r}")


def open_app(name: str) -> str:
    argv = resolve_command(name)
    target = argv[0]
    try:
        if WINDOWS and not os.path.isabs(target):
            os.startfile(target)  # type: ignore[attr-defined]  # ShellExecute: no shell parsing
        elif WINDOWS:
            subprocess.Popen(argv, close_fds=True)
        else:
            subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Unsafe(f"I couldn't find an app called {name!r}") from None
    except OSError as e:
        raise Unsafe(f"that wouldn't open: {e}") from None
    log.info("opened %s (%s)", name, target)
    return f"Opened {name}."


# ── closing apps ──
def _processes() -> list[Any]:
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is in requirements
        raise Unsafe("closing apps needs the psutil package; run 'make setup' again") from None
    return list(psutil.process_iter(["pid", "name"]))


def close_app(name: str) -> str:
    want = " ".join(str(name).split()).strip().lower()
    if not want:
        raise Unsafe("you didn't say which app")
    target = aliases().get(want, want).lower()
    stem = Path(target).stem.lower()
    if stem in PROTECTED_STEMS or Path(want).stem.lower() in PROTECTED_STEMS:
        raise Unsafe(f"I won't close {name}: the system needs it")

    me = os.getpid()
    hits = []
    for proc in _processes():
        pname = (proc.info.get("name") or "").lower()
        if not pname or proc.info["pid"] == me:
            continue
        if Path(pname).stem.lower() in PROTECTED_STEMS:
            continue
        if pname == target or Path(pname).stem.lower() == stem:
            hits.append(proc)
    if not hits:
        return f"{name} isn't running."

    closed = 0
    for proc in hits:
        try:
            proc.terminate()  # ask nicely first; a window app gets to save
            closed += 1
        except Exception as e:  # already gone, or not ours to touch
            log.info("couldn't close pid %s: %s", proc.info.get("pid"), e)
    gone, alive = _wait_gone(hits, 3.0)
    for proc in alive:
        try:
            proc.kill()
        except Exception:
            pass
    log.info("closed %s (%d process(es))", name, closed)
    return f"Closed {name}." if closed else f"I couldn't close {name}."


def _wait_gone(procs: list[Any], timeout: float) -> tuple[list[Any], list[Any]]:
    try:
        import psutil

        return psutil.wait_procs(procs, timeout=timeout)
    except Exception:
        time.sleep(timeout)
        return [], []


# ── files ──
def list_files(folder: str = "") -> str:
    d = safe_path(folder) if folder else workspace()
    if not d.exists():
        return f"There's no folder called {pretty(d)}."
    if not d.is_dir():
        return f"{pretty(d)} is a file, not a folder."
    rows = [e for e in sorted(d.iterdir(), key=lambda x: x.name.lower()) if e.name != ".trash"]
    if not rows:
        return f"{pretty(d) or 'Your workspace'} is empty."
    out = [f"{'[dir] ' if e.is_dir() else ''}{e.name}" for e in rows[:MAX_LIST]]
    more = f" (+{len(rows) - MAX_LIST} more)" if len(rows) > MAX_LIST else ""
    return f"{len(rows)} item(s) in {pretty(d) or 'your workspace'}: " + ", ".join(out) + more


def create_file(name: str, content: str = "", overwrite: bool = False) -> str:
    p = safe_path(name)
    text = str(content or "")
    if len(text) > MAX_WRITE:
        raise Unsafe(f"that's too long to write ({len(text)} characters)")
    if p.is_dir():
        raise Unsafe(f"{pretty(p)} is a folder")
    if p.exists() and not overwrite:
        return f"{pretty(p)} already exists. Ask the boss if you should overwrite it, then call again with overwrite=true."
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():  # keep the old copy; overwriting is destructive too
        _to_trash(p)
    p.write_text(text, encoding="utf-8")
    log.info("wrote %s (%d chars)", p, len(text))
    return f"Saved {pretty(p)} ({len(text)} characters) in your workspace."


# ── typing into whatever window is focused ──
MAX_TYPE = 2000  # characters accepted in one go
FOCUS_PAUSE = 1.2  # seconds between "go" and the first keystroke, so focus can settle


def focused_window() -> str:
    """Name of the window that would receive keystrokes right now, or '' if we can't tell.

    This is the safety story for typing: he says where the text is going before it goes there.
    """
    try:
        if WINDOWS:
            import ctypes

            u = ctypes.windll.user32
            h = u.GetForegroundWindow()
            n = u.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            return buf.value.strip()
        out = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=2
        )
        return out.stdout.strip()
    except Exception:  # no xdotool, no desktop, locked screen - not worth failing over
        return ""


def _looks_like_us(title: str) -> bool:
    return "boogeyman" in title.lower()


# target -> when it was armed
_armed_typing: dict[str, float] = {}


def type_text(text: str, confirmed: bool = False, press_enter: bool = False) -> str:
    """Type into whatever window is focused. Two-step, like deleting, and for the same reason:
    speech-to-text mishears, and these keystrokes land in somebody else's application."""
    body = str(text or "")
    if not body.strip():
        raise Unsafe("you didn't say what to type")
    if len(body) > MAX_TYPE:
        raise Unsafe(f"that's too long to type ({len(body)} characters)")

    where = focused_window()
    if _looks_like_us(where):
        raise Unsafe("the Boogeyman's own window is focused; give the other window focus first")

    key = body
    fresh = time.monotonic() - _armed_typing.get(key, 0.0) < ARM_WINDOW
    if not (confirmed and fresh):
        _armed_typing[key] = time.monotonic()
        target = f"'{where}'" if where else "whatever window you leave focused"
        enter = " and press Enter" if press_enter else ""
        preview = body if len(body) <= 120 else body[:120] + "..."
        return (
            f"Not typed yet. Ready to type {len(body)} characters{enter} into {target}. "
            f"Ask the boss exactly this: 'I'm about to type \"{preview}\" into {target}. Go ahead, boss?' "
            "If he says yes, call type_text again with confirmed=true."
        )

    _armed_typing.pop(key, None)
    try:
        from pynput.keyboard import Controller, Key
    except ImportError:
        raise Unsafe("typing needs the pynput package; run 'make setup' again") from None

    time.sleep(FOCUS_PAUSE)  # let him finish speaking and the window settle
    landed = focused_window()
    if _looks_like_us(landed):
        raise Unsafe("my own window took focus; nothing typed")
    kb = Controller()
    try:
        kb.type(body)
        if press_enter:
            kb.press(Key.enter)
            kb.release(Key.enter)
    except Exception as e:
        raise Unsafe(f"typing failed: {e}") from None
    log.info("typed %d chars into %r (enter=%s)", len(body), landed, press_enter)
    return f"Typed {len(body)} characters into {landed or 'the focused window'}."


# path -> when it was armed
_armed: dict[str, float] = {}


def _to_trash(p: Path) -> Path:
    dest = trash_dir() / f"{datetime.now():%Y%m%d-%H%M%S}-{p.name}"
    i = 1
    while dest.exists():
        dest = trash_dir() / f"{datetime.now():%Y%m%d-%H%M%S}-{i}-{p.name}"
        i += 1
    shutil.move(str(p), str(dest))
    return dest


def delete_file(name: str, confirmed: bool = False) -> str:
    """Two-step. The first call only arms it; the second actually moves it to trash."""
    p = safe_path(name)
    if not p.exists():
        return f"There's no {pretty(p)} in your workspace."
    if p.is_dir() and any(p.iterdir()):
        raise Unsafe(f"{pretty(p)} is a folder with things in it; I won't delete that by voice")

    key = str(p)
    armed_at = _armed.get(key, 0.0)
    fresh = time.monotonic() - armed_at < ARM_WINDOW
    if not (confirmed and fresh):
        # Arm it and make the model ask out loud. Claiming confirmed=true without having
        # asked first lands here too, which is the point: the gate is here, not in the prompt.
        _armed[key] = time.monotonic()
        size = p.stat().st_size if p.is_file() else 0
        return (
            f"Not deleted yet. {pretty(p)} ({size} bytes) is ready to go. Ask the boss exactly this: "
            f"'Should I delete {pretty(p)}, boss?' If he says yes, call delete_file again with confirmed=true."
        )
    _armed.pop(key, None)
    dest = _to_trash(p)
    log.info("deleted %s -> %s", p, dest)
    return f"Deleted {pretty(p)}. It's in the workspace trash folder if you want it back."
