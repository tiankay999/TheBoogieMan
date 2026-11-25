"""Tests for the hands: the guard rails matter more than the happy path."""

import os
import tempfile

os.environ["BOOGEYMAN_VOICE"] = "0"
os.environ["BOOGEYMAN_WORKSPACE"] = tempfile.mkdtemp()

import time  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

import hands  # noqa: E402


def ws() -> Path:
    return hands.workspace()


# ── confinement ──
@pytest.mark.parametrize(
    "escape",
    ["../outside.txt", "../../etc/passwd", "a/../../outside.txt", "~/secret.txt", "/etc/passwd", "C:\\Windows\\win.ini"],
)
def test_safe_path_refuses_escaping_the_workspace(escape):
    # ~ and absolute paths only pass if they happen to land inside the workspace.
    try:
        p = hands.safe_path(escape)
    except hands.Unsafe:
        return
    assert ws() == p or ws() in p.parents, f"{escape} escaped to {p}"


def test_safe_path_allows_names_inside():
    assert hands.safe_path("notes.txt") == ws() / "notes.txt"
    assert hands.safe_path("sub/deep/notes.txt") == ws() / "sub" / "deep" / "notes.txt"


def test_safe_path_rejects_empty():
    with pytest.raises(hands.Unsafe):
        hands.safe_path("   ")


# ── creating ──
def test_create_then_list():
    hands.create_file("ideas.txt", "jollof recipe")
    assert (ws() / "ideas.txt").read_text(encoding="utf-8") == "jollof recipe"
    assert "ideas.txt" in hands.list_files()


def test_create_makes_subfolders():
    hands.create_file("deep/nested/note.txt", "hi")
    assert (ws() / "deep" / "nested" / "note.txt").exists()


def test_create_refuses_silent_overwrite():
    hands.create_file("keep.txt", "original")
    out = hands.create_file("keep.txt", "replacement")
    assert "already exists" in out
    assert (ws() / "keep.txt").read_text(encoding="utf-8") == "original"


def test_overwrite_keeps_a_copy_in_trash():
    hands.create_file("replaced.txt", "v1")
    hands.create_file("replaced.txt", "v2", overwrite=True)
    assert (ws() / "replaced.txt").read_text(encoding="utf-8") == "v2"
    assert any("replaced.txt" in p.name for p in hands.trash_dir().iterdir())


def test_create_refuses_a_huge_write():
    with pytest.raises(hands.Unsafe):
        hands.create_file("big.txt", "x" * (hands.MAX_WRITE + 1))


# ── deleting: the two-step gate ──
def test_first_delete_call_only_arms_it():
    hands.create_file("doomed.txt", "bye")
    out = hands.delete_file("doomed.txt")
    assert "Not deleted yet" in out
    assert (ws() / "doomed.txt").exists(), "the first call must not delete anything"


def test_confirmed_delete_after_arming_moves_it_to_trash():
    hands.create_file("doomed2.txt", "bye")
    hands.delete_file("doomed2.txt")  # arm
    out = hands.delete_file("doomed2.txt", confirmed=True)
    assert "Deleted" in out
    assert not (ws() / "doomed2.txt").exists()
    assert any("doomed2.txt" in p.name for p in hands.trash_dir().iterdir()), "deletes must be recoverable"


def test_confirmed_true_without_arming_does_not_delete():
    """The gate lives in the code, not the prompt: a model that just claims confirmed=true fails."""
    hands.create_file("safe.txt", "still here")
    out = hands.delete_file("safe.txt", confirmed=True)
    assert "Not deleted yet" in out
    assert (ws() / "safe.txt").exists()


def test_arming_expires():
    hands.create_file("stale.txt", "x")
    hands.delete_file("stale.txt")
    hands._armed[str(ws() / "stale.txt")] = time.monotonic() - hands.ARM_WINDOW - 1
    out = hands.delete_file("stale.txt", confirmed=True)
    assert "Not deleted yet" in out
    assert (ws() / "stale.txt").exists()


def test_arming_one_file_does_not_confirm_another():
    hands.create_file("a.txt", "a")
    hands.create_file("b.txt", "b")
    hands.delete_file("a.txt")  # arm a
    out = hands.delete_file("b.txt", confirmed=True)  # try to ride it for b
    assert "Not deleted yet" in out
    assert (ws() / "b.txt").exists()


def test_delete_refuses_a_non_empty_folder():
    hands.create_file("folder/inside.txt", "x")
    with pytest.raises(hands.Unsafe):
        hands.delete_file("folder")


def test_delete_missing_file_is_harmless():
    assert "no" in hands.delete_file("never-existed.txt").lower()


# ── apps ──
def test_close_refuses_protected_processes():
    for name in ("explorer", "systemd", "boogeyman", "python"):
        with pytest.raises(hands.Unsafe):
            hands.close_app(name)


def test_open_refuses_shell_metacharacters():
    for bad in ("notepad & calc", "x; rm -rf /", "a | b", "$(whoami)", "a`b`"):
        with pytest.raises(hands.Unsafe):
            hands.resolve_command(bad)


def test_open_refuses_empty():
    with pytest.raises(hands.Unsafe):
        hands.resolve_command("  ")


def test_aliases_resolve_to_a_command():
    argv = hands.resolve_command("calculator" if hands.WINDOWS else "text editor")
    assert argv and isinstance(argv[0], str)


# ── typing into other windows ──
def test_typing_first_call_does_not_type(monkeypatch):
    typed = []
    monkeypatch.setattr(hands, "focused_window", lambda: "Google - Brave")
    out = hands.type_text("jollof recipe")
    assert "Not typed yet" in out
    assert "Google - Brave" in out, "he must name the window before typing into it"
    assert not typed


def test_typing_refuses_when_our_own_window_is_focused(monkeypatch):
    monkeypatch.setattr(hands, "focused_window", lambda: "The Boogeyman")
    with pytest.raises(hands.Unsafe):
        hands.type_text("hello")


def test_typing_refuses_empty_and_overlong(monkeypatch):
    monkeypatch.setattr(hands, "focused_window", lambda: "Notepad")
    with pytest.raises(hands.Unsafe):
        hands.type_text("   ")
    with pytest.raises(hands.Unsafe):
        hands.type_text("x" * (hands.MAX_TYPE + 1))


def test_confirmed_typing_without_arming_does_not_type(monkeypatch):
    """Same gate as deleting: claiming confirmed=true without asking first gets refused."""
    monkeypatch.setattr(hands, "focused_window", lambda: "Notepad")
    hands._armed_typing.clear()
    out = hands.type_text("rm -rf something", confirmed=True)
    assert "Not typed yet" in out


def test_armed_then_confirmed_types(monkeypatch):
    sent = {"text": "", "enter": False}

    class FakeKb:
        def type(self, s):
            sent["text"] += s

        def press(self, k):
            sent["enter"] = True

        def release(self, k):
            pass

    import types as _t

    fake = _t.ModuleType("pynput.keyboard")
    fake.Controller = FakeKb
    fake.Key = _t.SimpleNamespace(enter="enter")
    pkg = _t.ModuleType("pynput")
    pkg.keyboard = fake
    monkeypatch.setitem(__import__("sys").modules, "pynput", pkg)
    monkeypatch.setitem(__import__("sys").modules, "pynput.keyboard", fake)
    monkeypatch.setattr(hands, "focused_window", lambda: "Notepad")
    monkeypatch.setattr(hands, "FOCUS_PAUSE", 0)

    hands.type_text("hello world")  # arm
    out = hands.type_text("hello world", confirmed=True, press_enter=True)
    assert "Typed" in out
    assert sent["text"] == "hello world"
    assert sent["enter"] is True


def test_arming_one_phrase_does_not_confirm_another(monkeypatch):
    monkeypatch.setattr(hands, "focused_window", lambda: "Notepad")
    hands._armed_typing.clear()
    hands.type_text("safe phrase")
    out = hands.type_text("totally different phrase", confirmed=True)
    assert "Not typed yet" in out
