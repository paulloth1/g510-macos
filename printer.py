"""Read print status from a Creality printer on the local network.

The printer serves a WebSocket whose first frame is a complete snapshot of
every field it tracks; everything after that is deltas. Reading one snapshot
and closing costs about 30ms, so there is no persistent connection to manage -
just a poll on a cache.
"""
import json
import os
import time

import config

DEFAULT_PORT = 9999
CONNECT_TIMEOUT = 3.0

_cache = {"when": 0.0, "value": None, "error": None}


def settings():
    """The printer block from the config, whether or not it is filled in."""
    block = config.load().get("printer") or {}
    return {
        "enabled": bool(block.get("enabled")),
        "host": (block.get("host") or "").strip(),
        "port": int(block.get("port") or DEFAULT_PORT),
        "refresh": float(block.get("refresh_seconds") or 5.0),
    }


def status(force=False):
    """Latest snapshot, or None if the printer is off, unset or unreachable."""
    conf = settings()
    if not conf["enabled"] or not conf["host"]:
        return None
    now = time.time()
    if not force and now - _cache["when"] < conf["refresh"]:
        return _cache["value"]
    reading, error = _fetch(conf["host"], conf["port"])
    _cache.update(when=now, value=reading, error=error)
    return reading


def last_error():
    return _cache["error"]


def _fetch(host, port):
    try:
        import websocket
    except ImportError:
        return None, "websocket-client is not installed"
    url = f"ws://{host}:{port}"
    try:
        connection = websocket.create_connection(url, timeout=CONNECT_TIMEOUT)
    except Exception as exc:
        return None, f"could not reach {host}:{port} ({exc})"
    try:
        snapshot = json.loads(connection.recv())
    except (ValueError, OSError) as exc:
        return None, f"bad reply from {host} ({exc})"
    finally:
        try:
            connection.close()
        except Exception:
            pass
    if not isinstance(snapshot, dict):
        return None, "unexpected reply shape"
    return _normalise(snapshot), None


def _normalise(snapshot):
    """Pick out the handful of fields worth showing, with sane types."""
    def number(key, default=0.0):
        try:
            return float(snapshot.get(key, default) or 0)
        except (TypeError, ValueError):
            return default

    name = os.path.basename(str(snapshot.get("printFileName") or ""))
    for suffix in (".gcode", ".gco"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return {
        "model": snapshot.get("model") or snapshot.get("hostname") or "printer",
        "file": name,
        "progress": int(number("printProgress")),
        "left": int(number("printLeftTime")),
        "elapsed": int(number("printJobTime")),
        "layer": int(number("layer")),
        "layers": int(number("TotalLayer")),
        "nozzle": number("nozzleTemp"),
        "nozzle_target": number("targetNozzleTemp"),
        "bed": number("bedTemp0"),
        "bed_target": number("targetBedTemp0"),
        "state": int(number("state")),
    }


# Observed on a K1C rather than documented, so anything unrecognised is
# reported as its raw number instead of being guessed at.
STATES = {0: "idle", 1: "printing", 2: "paused", 3: "stopped"}


def describe_state(reading):
    """A short word for what the printer is doing."""
    if not reading:
        return "offline"
    state = reading.get("state")
    if state == 3 and reading.get("progress", 0) >= 100:
        return "done"
    return STATES.get(state, f"state {state}")


def is_printing(reading):
    return bool(reading) and reading.get("state") == 1
