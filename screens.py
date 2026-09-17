"""The things the LCD can show.

Each screen is a function taking (canvas, state) and drawing one frame.
State is a small dict the daemon keeps between refreshes.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

import config
import refresh
from device import LCD_WIDTH
from lcd import Canvas

_CPU_COUNT = os.cpu_count() or 1
_mem_cache = {"when": 0.0, "value": 0.0}
_batt_cache = {"when": 0.0, "value": None}

CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")
CLAUDE_STATUSLINE = os.path.expanduser("~/.claude/runcat-usage.json")
# Keyed on the source files' mtimes rather than a timer, so the screen follows
# whichever source last changed instead of lagging behind both.
_util_cache = {"key": None, "value": None}
MEDIA_REFRESH = 3.0

# launchd gives the agent a minimal PATH with no Homebrew on it, so the bare
# name is not enough: unresolved, the media screen silently falls back to a
# browser window title, which carries no duration and so draws no progress bar.
NOWPLAYING_CANDIDATES = (
    "/opt/homebrew/bin/nowplaying-cli",
    "/usr/local/bin/nowplaying-cli",
)


def _find_nowplaying():
    """Look inside the app bundle first, then the usual install locations.

    A packaged app cannot rely on PATH at all - launchd gives an agent a
    minimal one, and a Mac that installed the .app may have no Homebrew.
    """
    bundled = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                           "nowplaying-cli")
    if os.path.exists(bundled):
        return bundled
    for path in NOWPLAYING_CANDIDATES:
        if os.path.exists(path):
            return path
    return shutil.which("nowplaying-cli")


NOWPLAYING_CLI = _find_nowplaying()

# Ask only players that are already running, so nothing gets launched.
_TRACK_SCRIPT = "\n".join([
    'on trackFor(appName)',
    '  tell application "System Events"',
    '    if (count of (every process whose name is appName)) is 0 then return ""',
    '  end tell',
    '  tell application appName',
    '    if player state is not playing then return ""',
    '    return (get artist of current track) & " - " & (get name of current track)',
    '  end tell',
    'end trackFor',
    'set out to trackFor("Music")',
    'if out is "" then set out to trackFor("Spotify")',
    'return out',
])


def load_fraction():
    """1-minute load average as a fraction of available cores."""
    return min(1.0, os.getloadavg()[0] / _CPU_COUNT)


def memory_fraction():
    """Share of physical memory in use, refreshed at most once a second."""
    now = time.time()
    if now - _mem_cache["when"] < 1.0:
        return _mem_cache["value"]
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True,
                             timeout=2).stdout
        counts = {}
        for line in out.splitlines():
            match = re.match(r'Pages (.+?):\s+(\d+)', line)
            if match:
                counts[match.group(1)] = int(match.group(2))
        used = counts.get("active", 0) + counts.get("wired down", 0)
        total = used + counts.get("free", 0) + counts.get("inactive", 0)
        value = used / total if total else 0.0
    except (subprocess.SubprocessError, OSError, ValueError):
        value = 0.0
    _mem_cache.update(when=now, value=value)
    return value


def battery():
    """(percent, charging) or None on a desktop, refreshed every 30s."""
    now = time.time()
    if now - _batt_cache["when"] < 30.0:
        return _batt_cache["value"]
    value = None
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True,
                             text=True, timeout=2).stdout
        match = re.search(r'(\d+)%', out)
        if match:
            value = (int(match.group(1)), "AC Power" in out or "charging" in out)
    except (subprocess.SubprocessError, OSError):
        value = None
    _batt_cache.update(when=now, value=value)
    return value


_media_source = refresh.Background(
    lambda: (_from_nowplaying() or _from_players() or _from_window_title(), None),
    MEDIA_REFRESH)


def now_playing():
    """What is playing, from whichever source can see it.

    macOS's own now-playing information covers every player including browsers,
    which the per-app AppleScript route cannot. Asking costs ~100ms, so it is
    fetched off-thread and the elapsed time advanced locally in between - which
    also makes the progress bar move smoothly rather than in three-second steps.
    """
    reading = _media_source.get()
    return _advance(reading, time.time() - _media_source.fetched)


def _advance(reading, seconds):
    """Move a cached reading's elapsed time forward without re-polling."""
    if not reading or not reading.get("playing") or not reading.get("duration"):
        return reading
    moved = dict(reading)
    moved["elapsed"] = min(reading["duration"],
                           (reading.get("elapsed") or 0) + seconds)
    return moved


def _from_nowplaying():
    """macOS now-playing info, covering browsers and every other player."""
    if not NOWPLAYING_CLI:
        return None
    try:
        result = subprocess.run([NOWPLAYING_CLI, "get-raw"],
                                capture_output=True, text=True, timeout=3)
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        info = json.loads(result.stdout or "{}")
    except ValueError:
        return None
    title = info.get("kMRMediaRemoteNowPlayingInfoTitle")
    if not title:
        return None
    bundle = info.get("kMRMediaRemoteNowPlayingInfoClientBundleIdentifier") or ""
    return {
        "title": title,
        "artist": info.get("kMRMediaRemoteNowPlayingInfoArtist") or "",
        "elapsed": info.get("kMRMediaRemoteNowPlayingInfoElapsedTime") or 0,
        "duration": info.get("kMRMediaRemoteNowPlayingInfoDuration") or 0,
        "playing": bool(info.get("kMRMediaRemoteNowPlayingInfoPlaybackRate")),
        "app": bundle.rsplit(".", 1)[-1],
    }


def _from_players():
    """Music or Spotify directly, if the system route gave nothing."""
    try:
        result = subprocess.run(["osascript", "-e", _TRACK_SCRIPT],
                                capture_output=True, text=True, timeout=3)
    except (subprocess.SubprocessError, OSError):
        return None
    line = result.stdout.strip()
    if not line:
        return None
    artist, _, title = line.partition(" - ")
    if not title:
        artist, title = "", line
    return {"title": title.strip(), "artist": artist.strip(), "elapsed": 0,
            "duration": 0, "playing": True, "app": ""}


def _from_window_title():
    """Last resort: a browser's window title names the page that is playing."""
    script = ('tell application "System Events" to tell process "firefox" '
              'to return name of front window')
    try:
        result = subprocess.run(["osascript", "-e", script],
                                capture_output=True, text=True, timeout=3)
    except (subprocess.SubprocessError, OSError):
        return None
    title = result.stdout.strip()
    if not title or "YouTube" not in title:
        return None
    return {"title": title.rsplit(" - YouTube", 1)[0], "artist": "YouTube",
            "elapsed": 0, "duration": 0, "playing": True, "app": "firefox"}


def clock(seconds):
    """612 -> '10:12'."""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def claude_utilization():
    """Percentage of the Claude usage limits consumed.

    Two sources carry these numbers and they go stale at very different rates:
    Claude Code's own cache in ~/.claude.json only refreshes now and then,
    while the statusline dump is rewritten on every render. Neither is
    authoritative, so take whichever was written most recently.
    """
    key = tuple(_mtime(path) for path in (CLAUDE_CONFIG, CLAUDE_STATUSLINE))
    if key == _util_cache["key"] and _util_cache["value"]:
        return _mark_stale(_util_cache["value"])

    candidates = [reading for reading in (_from_claude_config(),
                                          _from_statusline()) if reading]
    if candidates:
        best = max(candidates, key=lambda reading: reading["fetched"])
    else:
        best = {"five_hour": None, "seven_day": None, "resets_at": None,
                "context": None, "stale": True, "source": None, "fetched": 0.0}
    _util_cache.update(key=key, value=best)
    return _mark_stale(best)


CLAUDE_ROWS = {
    "five_hour": "5h",
    "seven_day": "7d",
    "context": "ctx",
}


def _mark_stale(reading):
    """Age the reading now, not when it was cached.

    If neither source file changes again the cached entry is returned for
    ever, so staleness has to be judged on the way out or it can never become
    true.
    """
    block = config.load().get("claude") or {}
    limit = float(block.get("stale_after_seconds") or 600)
    reading["stale"] = (time.time() - (reading.get("fetched") or 0)) > limit
    return reading


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _from_claude_config():
    """Claude Code's own cached utilization, with the time it was fetched."""
    try:
        with open(CLAUDE_CONFIG) as handle:
            cached = json.load(handle).get("cachedUsageUtilization") or {}
    except (OSError, ValueError):
        return None
    buckets = cached.get("utilization") or {}
    five = buckets.get("five_hour") or {}
    seven = buckets.get("seven_day") or {}
    if five.get("utilization") is None:
        return None
    return {
        "five_hour": int(five["utilization"]),
        "seven_day": (int(seven["utilization"])
                      if seven.get("utilization") is not None else None),
        "resets_at": five.get("resets_at"),
        "context": None,
        "fetched": (cached.get("fetchedAtMs") or 0) / 1000.0,
        "source": "config",
        "stale": False,
    }


def _from_statusline():
    """The statusline dump, which is rewritten on every render."""
    try:
        with open(CLAUDE_STATUSLINE) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    reading = {"five_hour": None, "seven_day": None, "resets_at": None,
               "context": None, "source": "statusline", "stale": False,
               "fetched": _mtime(CLAUDE_STATUSLINE)}
    for metric in data.get("metrics") or []:
        value = metric.get("normalizedValue")
        if value is None:
            continue
        title = metric.get("title")
        if title == "5h":
            reading["five_hour"] = int(round(value * 100))
        elif title == "7d":
            reading["seven_day"] = int(round(value * 100))
        elif title == "Context":
            reading["context"] = int(round(value * 100))
    if reading["five_hour"] is None:
        return None
    return reading


def until(stamp):
    """'1h23m' until an ISO timestamp, or None."""
    if not stamp:
        return None
    try:
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    if seconds <= 0:
        return "now"
    hours, minutes = int(seconds // 3600), int((seconds % 3600) // 60)
    if hours >= 24:
        return f"{hours // 24}d{hours % 24}h"
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def frontmost_app():
    """Name of the frontmost application, or None."""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return str(app.localizedName()) if app else None
    except Exception:
        return None


# -- screens ---------------------------------------------------------------

def screen_clock(canvas, _state):
    canvas.centered(time.strftime("%H:%M:%S"), 1, 22)
    canvas.centered(time.strftime("%a %d %b %Y"), 28, 12)


def screen_status(canvas, _state):
    canvas.text(time.strftime("%H:%M:%S"), 2, 0, 15)
    power = battery()
    if power:
        percent, charging = power
        label = f"{percent}%{'+' if charging else ''}"
        canvas.text(label, LCD_RIGHT - canvas.measure(label, 11), 2, 11)
    canvas.text("LOAD", 2, 18, 9)
    canvas.bar(34, 18, 124, 9, load_fraction())
    canvas.text("MEM", 2, 30, 9)
    canvas.bar(34, 30, 124, 9, memory_fraction())


def screen_gkeys(canvas, state):
    canvas.text("G-KEYS", 2, 0, 11)
    canvas.text(time.strftime("%H:%M"), 120, 0, 11)
    last = state.get("last_gkey")
    if last:
        canvas.text(last, 2, 14, 18)
        canvas.text(_fit(canvas, state.get("last_action", "unbound"), 9),
                    2, 32, 9)
    else:
        canvas.text("press a G-key", 2, 20, 11)


def screen_app(canvas, _state):
    name = frontmost_app() or "-"
    canvas.text(time.strftime("%H:%M:%S"), 2, 0, 15)
    canvas.text(name[:22], 2, 20, 12)
    canvas.bar(2, 34, 156, 8, load_fraction())


def screen_claude(canvas, _state):
    """Percentage of the Claude usage limits used, with time to reset."""
    util = claude_utilization()
    canvas.text("CLAUDE", 2, 0, 11)
    context = util.get("context")
    resets = until(util.get("resets_at"))
    if context is not None:
        header = f"ctx {context}%"
    elif resets:
        header = f"resets {resets}"
    else:
        header = time.strftime("%H:%M")
    if util.get("stale"):
        header = "~ " + header
    canvas.text(header, LCD_WIDTH - canvas.measure(header, 10) - 2, 1, 10)

    block = config.load().get("claude") or {}
    wanted = [row for row in (block.get("rows") or ["five_hour", "seven_day"])
              if row in CLAUDE_ROWS][:2]
    for index, key in enumerate(wanted or ["five_hour", "seven_day"]):
        label = CLAUDE_ROWS[key]
        y = 15 + index * 15
        value = util.get(key)
        canvas.text(label, 2, y + 1, 10)
        if value is None:
            canvas.text("no data", 24, y + 1, 10)
            continue
        canvas.bar(22, y, 104, 11, value / 100.0)
        percent = f"{value}%"
        canvas.text(percent, LCD_WIDTH - canvas.measure(percent, 11) - 2, y, 11)


def screen_printer(canvas, _state):
    """3D printer progress: what it is doing, how far in, and how hot."""
    import printer

    reading = printer.status()
    if not reading:
        canvas.text("PRINTER", 2, 0, 11)
        canvas.text(printer.describe_state(None), 2, 16, 12)
        note = printer.last_error()
        if note:
            canvas.text(_fit(canvas, note, 9), 2, 31, 9)
        return

    state = printer.describe_state(reading).upper()
    canvas.text(f"{reading['model']}  {state}", 2, 0, 10)
    if printer.is_printing(reading) and reading["left"]:
        left = _duration(reading["left"])
        canvas.text(left, LCD_WIDTH - canvas.measure(left, 10) - 2, 1, 10)

    canvas.text(_fit(canvas, reading["file"] or "no file", 11), 2, 11, 11)

    detail = f"N{reading['nozzle']:.0f}  B{reading['bed']:.0f}"
    if reading["layers"]:
        detail += f"  L{reading['layer']}/{reading['layers']}"
    canvas.text(detail, 2, 24, 9)
    percent = f"{reading['progress']}%"
    canvas.text(percent, LCD_WIDTH - canvas.measure(percent, 9) - 2, 24, 9)

    canvas.bar(2, 36, LCD_WIDTH - 4, 6, reading["progress"] / 100.0)


def _duration(seconds):
    """1720 -> '28m', 7200 -> '2h00'."""
    seconds = max(0, int(seconds))
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    return f"{hours}h{minutes:02d}" if hours else f"{minutes}m"


def screen_media(canvas, _state):
    track = now_playing()
    if not track:
        canvas.text("NOW PLAYING", 2, 0, 11)
        canvas.text(time.strftime("%H:%M"), 122, 0, 11)
        canvas.text("nothing playing", 2, 18, 12)
        return

    header = "NOW PLAYING" if track.get("playing") else "PAUSED"
    canvas.text(header, 2, 0, 10)
    duration = track.get("duration") or 0
    if duration:
        stamp = f"{clock(track.get('elapsed') or 0)} / {clock(duration)}"
    else:
        stamp = time.strftime("%H:%M")
    canvas.text(stamp, LCD_WIDTH - canvas.measure(stamp, 10) - 2, 1, 10)

    canvas.text(_fit(canvas, track["title"], 13), 2, 12, 13)
    artist = track.get("artist") or track.get("app") or ""
    if artist:
        canvas.text(_fit(canvas, artist, 10), 2, 26, 10)
    if duration:
        canvas.bar(2, 38, LCD_WIDTH - 4, 5,
                   (track.get("elapsed") or 0) / duration)


def _fit(canvas, string, size):
    """Trim a string to the panel width, with an ellipsis if it had to go."""
    if canvas.measure(string, size) <= LCD_WIDTH - 4:
        return string
    while string and canvas.measure(string + "...", size) > LCD_WIDTH - 4:
        string = string[:-1]
    return string + "..."


def screen_record(canvas, state):
    """Shown while MR recording is armed or running."""
    stage = state.get("record_state")
    if stage == "await_key":
        canvas.text("RECORD", 2, 0, 11)
        canvas.text("press a G-key", 2, 15, 13)
        canvas.text("MR again to cancel", 2, 31, 10)
    else:
        target = state.get("record_target", "?")
        canvas.text(f"RECORDING {target}", 2, 0, 11)
        canvas.text(f"{state.get('record_steps', 0)} keys", 2, 15, 14)
        canvas.text("MR or Esc to finish", 2, 31, 10)


LCD_RIGHT = 158

SCREENS = {
    "status": screen_status,
    "clock": screen_clock,
    "claude": screen_claude,
    "media": screen_media,
    "printer": screen_printer,
    "gkeys": screen_gkeys,
    "app": screen_app,
}

# Not in the cycle: the daemon switches to it while MR recording is active.
SCREENS_INTERNAL = {"record": screen_record}


def render(name, state):
    """Draw the named screen and return packed pixel rows."""
    canvas = Canvas()
    drawer = SCREENS.get(name) or SCREENS_INTERNAL.get(name) or screen_status
    drawer(canvas, state)
    return canvas.pixels()
