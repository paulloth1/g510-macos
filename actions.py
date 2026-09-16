"""Turn a G-key binding into something that actually happens on the Mac.

Synthetic keystrokes go through CGEvent, which needs this process to hold the
Accessibility permission. Everything else is plain subprocess work.
"""
import subprocess
import time

import Quartz

# macOS virtual key codes, keyed by the names used in the config file.
KEY_CODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41,
    "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "esc": 53, "forwarddelete": 117,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
}

MODIFIER_FLAGS = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "option": Quartz.kCGEventFlagMaskAlternate,
    "opt": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "control": Quartz.kCGEventFlagMaskControl,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}


class ActionError(Exception):
    pass


def can_post_events():
    """True if this process may synthesise keystrokes (Accessibility)."""
    try:
        return bool(Quartz.CGPreflightPostEventAccess())
    except AttributeError:
        return True          # older macOS without the preflight call


def request_post_access():
    """Ask macOS for Accessibility, raising the System Settings prompt."""
    try:
        return bool(Quartz.CGRequestPostEventAccess())
    except AttributeError:
        return True


def can_read_input():
    """True if this process may read HID input (Input Monitoring)."""
    try:
        return bool(Quartz.CGPreflightListenEventAccess())
    except AttributeError:
        return True


NEEDS_ACCESSIBILITY = ("keys", "text", "macro")


class PermissionError_(ActionError):
    """Raised when a binding needs Accessibility and it has not been granted."""


def parse_chord(chord):
    """'cmd+shift+4' -> (keycode, flags)."""
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        raise ActionError(f"Empty key chord: {chord!r}")
    flags = 0
    for part in parts[:-1]:
        if part not in MODIFIER_FLAGS:
            raise ActionError(f"Unknown modifier {part!r} in {chord!r}")
        flags |= MODIFIER_FLAGS[part]
    key = parts[-1]
    if key not in KEY_CODES:
        raise ActionError(f"Unknown key {key!r} in {chord!r}")
    return KEY_CODES[key], flags


def send_chord(chord):
    keycode, flags = parse_chord(chord)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def type_text(text):
    """Type a literal string, independent of keyboard layout."""
    for char in text:
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(char), char)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def run_shell(command):
    subprocess.Popen(command, shell=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def launch_app(name):
    subprocess.Popen(["open", "-a", name],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def play_macro(steps):
    """Replay a recorded sequence of chords with their original timing."""
    for step in steps:
        delay = float(step.get("delay", 0) or 0)
        if delay > 0:
            time.sleep(min(delay, 2.0))      # cap, so a pause cannot hang
        chord = step.get("keys")
        if chord:
            send_chord(chord)


def dispatch(binding, macros=None):
    """Run one binding dict, e.g. {"type": "keys", "keys": "cmd+c"}."""
    if not binding:
        return
    kind = binding.get("type")
    if kind in NEEDS_ACCESSIBILITY and not can_post_events():
        raise PermissionError_(
            f"a {kind!r} binding needs Accessibility permission - run: g510 permissions")
    if kind == "keys":
        send_chord(binding["keys"])
    elif kind == "text":
        type_text(binding["text"])
    elif kind == "shell":
        run_shell(binding["command"])
    elif kind == "app":
        launch_app(binding["name"])
    elif kind == "macro":
        name = binding.get("name")
        steps = (macros or {}).get(name)
        if not steps:
            raise ActionError(f"No recorded macro called {name!r}")
        play_macro(steps)
    else:
        raise ActionError(f"Unknown binding type {kind!r}")


def describe(binding):
    """Short human-readable summary of a binding, for menus and listings."""
    if not binding:
        return "-"
    kind = binding.get("type")
    if kind == "keys":
        return binding.get("keys", "?")
    if kind == "text":
        text = binding.get("text", "")
        return f'type "{text[:18]}{"..." if len(text) > 18 else ""}"'
    if kind == "shell":
        command = binding.get("command", "")
        return f"$ {command[:22]}{'...' if len(command) > 22 else ''}"
    if kind == "app":
        return f"open {binding.get('name', '?')}"
    if kind == "macro":
        return f"macro {binding.get('name', '?')}"
    return str(kind)
