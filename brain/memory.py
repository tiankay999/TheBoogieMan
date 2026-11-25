"""Long-term memory: facts about the boss + recent conversation, persisted on disk.

Stored at $BOOGEYMAN_DATA (default ~/.local/share/boogeyman)/memory.json, outside the repo,
so it never gets committed.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("brain.memory")

MAX_HISTORY = 30  # messages kept across restarts
MAX_FACTS = 300


def data_dir() -> Path:
    base = os.environ.get("BOOGEYMAN_DATA") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "boogeyman"
    )
    return Path(base)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Memory:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "memory.json"
        self.lock = threading.Lock()
        self.facts: list[dict[str, str]] = []
        self.history: list[dict[str, str]] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.facts = [f for f in data.get("facts", []) if f.get("text")]
            self.history = [m for m in data.get("history", []) if m.get("role") in ("user", "assistant")]
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # Keep a copy of a broken file instead of silently losing it.
            try:
                self.path.rename(self.path.with_suffix(".broken.json"))
            except OSError:
                pass

    def save(self) -> None:
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                # Always UTF-8. The default is the locale codec, which on Windows is cp1252 and
                # cannot encode Pidgin, Thai, emoji or smart quotes.
                tmp.write_text(
                    json.dumps({"facts": self.facts, "history": self.history}, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                tmp.replace(self.path)  # atomic
            except (OSError, ValueError) as e:
                # Failing to write is bad; losing his voice is worse. A raise here used to escape
                # reply(), kill the turn, and then kill every turn after it, because the text it
                # choked on stayed in history. Log it and keep talking.
                log.warning("couldn't save memory: %s", e)

    # ── facts ──
    def remember(self, text: str) -> dict[str, str]:
        text = " ".join(text.split()).strip()
        if not text:
            raise ValueError("empty fact")
        for f in self.facts:
            if f["text"].lower() == text.lower():
                return f
        fact = {"id": uuid.uuid4().hex[:8], "text": text, "at": now()}
        self.facts.append(fact)
        self.facts = self.facts[-MAX_FACTS:]
        self.save()
        return fact

    def forget(self, query: str) -> list[dict[str, str]]:
        """Remove facts whose id matches or whose text contains the query (case-insensitive)."""
        q = query.lower().strip()
        if not q:
            return []
        gone = [f for f in self.facts if f["id"] == q or q in f["text"].lower()]
        if gone:
            self.facts = [f for f in self.facts if f not in gone]
            self.save()
        return gone

    # ── conversation ──
    def add_turn(self, user: str, assistant: str) -> None:
        self.history += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
        self.history = self.history[-MAX_HISTORY:]
        self.save()

    def clear_history(self) -> None:
        self.history = []
        self.save()

    def snapshot(self) -> dict[str, Any]:
        return {"facts": list(self.facts), "turns": len(self.history) // 2}
