"""Config file handling for the G510 tools.

Lives at ~/.config/g510/config.json so it survives reinstalls of the app
directory, and is plain JSON so it can be edited by hand.
"""
import copy
import json
import os
import tempfile

CONFIG_DIR = os.path.expanduser("~/.config/g510")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "backlight": "#ffffff",
    "brightness": 100,
    "restore_backlight_on_start": True,
    "lcd": {
        "enabled": True,
        "screen": "status",
        "refresh_seconds": 1.0,
        "cycle": ["status", "clock", "claude", "media", "app"],
    },
    # The keys around the display. "next"/"prev" walk the cycle above,
    # "screen:<name>" jumps straight to one, "off" blanks the display.
    "lcd_keys": {
        "L1": "next",
        "L2": "screen:status",
        "L3": "screen:clock",
        "L4": "screen:claude",
        "L5": "screen:media",
    },
    # A Creality printer on the LAN. Its WebSocket's first frame is a full
    # status snapshot, so nothing is held open.
    "printer": {
        "enabled": False,
        "host": "",
        "port": 9999,
        "refresh_seconds": 5.0,
    },
    # Which usage windows the Claude screen shows, top to bottom. Any two of
    # five_hour, seven_day, context.
    "claude": {
        "rows": ["five_hour", "seven_day"],
        "stale_after_seconds": 600,
    },
    "app_colors": {},
    "app_profiles": {},
    "macros": {},
    "active_bank": "1",
    # macOS assumes this keyboard is ANSI because it declares no locale, which
    # swaps ^/° with <>| on an ISO board. See control.iso_swap.
    "iso_keyboard": False,
    # macOS drives Caps Lock and leaves the other two lock LEDs dark, so they
    # are free. Each can be: off, printing, claude, recording.
    "indicators": {"numlock": "off", "scrolllock": "off"},
    # The joystick switch on the top left is Logitech's game-mode switch. It
    # reports over USB, so it can drive anything: "bank:N", "screen:<name>",
    # "color:<colour>", or a binding object.
    "game_switch": {"on": "bank:3", "off": "bank:1"},
    "bindings": {
        "G1": {"type": "app", "name": "Safari"},
        "G2": {"type": "app", "name": "Terminal"},
        "G3": {"type": "keys", "keys": "cmd+shift+4"},
        "G4": {"type": "keys", "keys": "cmd+space"},
    },
}


def _merge(base, override):
    """Recursive dict merge, so a partial config file still gets all defaults.

    The base is deep-copied: callers mutate nested dicts like `lcd` and
    `macros` in place, and sharing them with DEFAULTS would quietly stop the
    defaults being the defaults for the rest of the process.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


BANKS = ("1", "2", "3")


def _migrate(settings, raw=None):
    """Move a flat `bindings` set into bank 1, the first time banks are used.

    The M1/M2/M3 keys pick between three independent sets of G-key bindings,
    the way Logitech's own software did. `bindings` stays as a live view of the
    active bank so older config files and callers keep working.
    """
    banks = settings.get("banks")
    if not isinstance(banks, dict):
        banks = {}
    for name in BANKS:
        if not isinstance(banks.get(name), dict):
            banks[name] = {}
    # Only a file that predates banks gets its flat `bindings` migrated. After
    # _merge has run, DEFAULTS' bindings are indistinguishable from the user's
    # own - so the decision is made on the raw file contents, or this quietly
    # restores the factory bindings every time bank 1 is emptied.
    legacy = raw is None or ("bindings" in raw and "banks" not in raw)
    if legacy and settings.get("bindings") and not banks["1"]:
        banks["1"] = dict(settings["bindings"])
    settings["banks"] = banks
    active = str(settings.get("active_bank", "1"))
    settings["active_bank"] = active if active in BANKS else "1"
    settings["bindings"] = banks[settings["active_bank"]]
    return settings


# Set when the config on disk could not be parsed, so callers can say so
# instead of pretending the defaults are what the user asked for.
last_error = None
_last_good = None


def load():
    """Read the config, surviving a broken file.

    The config is meant to be hand-edited, and a half-saved file should not
    take down a long-running agent or the GUI. A parse failure falls back to
    the last good config, or the defaults, and is recorded in `last_error`.
    """
    global last_error, _last_good
    if not os.path.exists(CONFIG_PATH):
        last_error = None
        return _migrate(copy.deepcopy(DEFAULTS))
    try:
        with open(CONFIG_PATH) as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("the config must be a JSON object")
        settings = _migrate(_merge(DEFAULTS, raw), raw)
    except (json.JSONDecodeError, ValueError) as exc:
        last_error = f"{CONFIG_PATH} is not valid JSON ({exc})"
        return (copy.deepcopy(_last_good) if _last_good
                else _migrate(copy.deepcopy(DEFAULTS)))
    except OSError as exc:
        last_error = f"Could not read {CONFIG_PATH}: {exc}"
        return (copy.deepcopy(_last_good) if _last_good
                else _migrate(copy.deepcopy(DEFAULTS)))
    last_error = None
    _last_good = copy.deepcopy(settings)
    return settings


def set_bank(settings, name):
    """Switch the active bank, pointing `bindings` at it."""
    name = str(name)
    if name not in BANKS:
        raise ValueError(f"Bank must be one of {', '.join(BANKS)}")
    settings["active_bank"] = name
    settings["bindings"] = settings.setdefault("banks", {}).setdefault(name, {})
    return settings


def save(config, force=False):
    """Write the config out.

    Refuses while the file on disk is unparseable unless forced, so a
    half-finished hand edit is not silently clobbered by a background write.
    """
    if last_error and not force:
        return False
    # `bindings` is a live view of the active bank, rebuilt on every load.
    # Writing it as well would leave two copies to drift apart, so the bank
    # is the only thing persisted.
    payload = dict(config)
    banks = payload.get("banks")
    if isinstance(banks, dict):
        banks = dict(banks)
        banks[str(payload.get("active_bank", "1"))] = payload.get("bindings", {})
        payload["banks"] = banks
    payload.pop("bindings", None)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    # A unique temp file per write: the agent, the CLI and the GUI all save
    # here, and a shared name would let two writers interleave into the same
    # file and defeat the atomic rename.
    handle_fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".config-")
    try:
        with os.fdopen(handle_fd, "w") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def ensure_exists():
    """Write out the defaults the first time, so there is something to edit.

    Migrated first: save() persists banks and drops the derived `bindings`
    view, so writing raw DEFAULTS produces a starter file with no G-key
    section at all.
    """
    if not os.path.exists(CONFIG_PATH):
        save(_migrate(copy.deepcopy(DEFAULTS)))
        return True
    return False


# -- colour helpers --------------------------------------------------------

NAMED_COLORS = {
    "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255),
    "cyan": (0, 255, 255), "magenta": (255, 0, 255), "yellow": (255, 255, 0),
    "orange": (255, 90, 0), "purple": (140, 0, 255), "pink": (255, 60, 140),
    "white": (255, 255, 255), "off": (0, 0, 0), "black": (0, 0, 0),
}


class ColorError(Exception):
    pass


def parse_color(text):
    """'#rrggbb' or a colour name -> (r, g, b)."""
    if text is None:
        raise ColorError("No colour given")
    key = str(text).strip().lower()
    if key in NAMED_COLORS:
        return NAMED_COLORS[key]
    hexpart = key.lstrip("#")
    if len(hexpart) == 3:
        hexpart = "".join(ch * 2 for ch in hexpart)
    if len(hexpart) != 6:
        raise ColorError(
            f"Bad colour {text!r}. Use #rrggbb or: {', '.join(sorted(NAMED_COLORS))}")
    try:
        return tuple(int(hexpart[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise ColorError(f"Bad colour {text!r}. Use #rrggbb or a colour name")


def format_color(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def apply_brightness(rgb, brightness):
    """Scale a colour by a 0-100 brightness.

    The G510 has no separate brightness register - the backlight is just the
    RGB value - so brightness is a scale factor on the chosen colour.
    """
    try:
        level = int(brightness)
    except (TypeError, ValueError):
        level = 100
    factor = max(0, min(100, level)) / 100.0
    return tuple(int(round(channel * factor)) for channel in rgb)


def effective_color(settings):
    """The colour actually sent to the keyboard, brightness included."""
    rgb = parse_color(settings.get("backlight", "#ffffff"))
    return apply_brightness(rgb, settings.get("brightness", 100))
