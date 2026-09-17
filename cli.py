"""Command line front end for the G510 tools."""
import collections
import os
import subprocess
import sys

import actions
import config
import control
import device
import ipc
import recorder
import screens

LABEL = "com.g510.agent"
PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(APP_DIR, "venv", "bin", "python")

USAGE = """g510 - control a Logitech G510 keyboard on macOS

  g510 info                     device, interfaces, current state
  g510 color [colour]           show or set the backlight (#rrggbb or a name)
  g510 brightness [0-100]       show or set backlight brightness
  g510 poweron <colour>         colour the keyboard powers on with
  g510 mkeys [m1 m2 m3 mr]      light M-key LEDs (no args clears them)
  g510 watch [--raw]            print G-key presses live

  g510 lcd <screen>             draw a screen once: clock status gkeys app
  g510 lcd text "a" "b" "c"     draw up to 3 lines of text
  g510 lcd clear                blank the display
  g510 next                     step to the next screen in the cycle

  g510 bank [1|2|3]             show or switch the binding bank (M1/M2/M3)
  g510 switch [on|off <action>] what the joystick switch does
  g510 printer [host|off]       3D printer the display reads from
  g510 claude [rows...]         which usage windows the Claude screen shows
  g510 bindings                 list G-key bindings for the active bank
  g510 bind G1 app Safari       bind a key: app | keys | shell | text | none
  g510 record G5 [name]         record a keystroke macro onto a G-key
  g510 macros [delete <name>]   list or remove recorded macros
  g510 profile "App" G1 ...     per-app binding overrides
  g510 config                   path to the config file
  g510 permissions              check/request the macOS permissions needed

  g510 start | stop | status    run the background agent at login
  g510 run                      run the agent in the foreground (ctrl-C stops)
  g510 gui                      menu bar app

colours: """ + ", ".join(sorted(config.NAMED_COLORS))


def _gkey_order(name):
    """Sort G1..G18 numerically, tolerating junk from a hand-edited config."""
    digits = name[1:] if name[:1].upper() == "G" else ""
    return (0, int(digits)) if digits.isdigit() else (1, name)


def save_config(settings):
    """Persist, or stop with the reason. A refused save is silent otherwise."""
    if not config.save(settings):
        sys.exit(config.last_error or f"Could not write {config.CONFIG_PATH}")


def guard(work):
    """Run a control-layer call, turning failures into clean CLI errors."""
    try:
        return work()
    except control.ControlError as exc:
        sys.exit(str(exc))


# -- commands --------------------------------------------------------------

def cmd_info(_args):
    import hid
    manufacturer, product = guard(control.device_info)
    print(f"  {manufacturer} {product}")
    print(f"  backlight:  {config.format_color(guard(control.get_backlight))}")
    for pid in device.PRODUCT_IDS:
        for found in hid.enumerate(device.VENDOR_ID, pid):
            mark = " <- control" if found["usage_page"] == 0xFF00 else ""
            print(f"  interface {found['interface_number']}  "
                  f"usage_page=0x{found['usage_page']:04x}{mark}")
    if not device.NON_EXCLUSIVE:
        print("  warning:    could not open non-exclusively; the media and "
              "volume keys may not work while the agent runs")
    print(f"  config:     {config.CONFIG_PATH}")
    print(f"  agent:      {'running' if control.daemon_running() else 'not running'}")


def cmd_color(args):
    if not args:
        print(config.format_color(guard(control.get_backlight)))
        return
    try:
        rgb = config.parse_color(args[0])
    except config.ColorError as exc:
        sys.exit(str(exc))
    settings = config.load()
    settings["backlight"] = config.format_color(rgb)
    save_config(settings)
    guard(lambda: control.set_backlight(config.effective_color(settings)))
    reload_agent()
    brightness = settings.get("brightness", 100)
    suffix = "" if brightness == 100 else f" at {brightness}% brightness"
    print(f"backlight -> {config.format_color(rgb)}{suffix}")


def cmd_poweron(args):
    if not args:
        sys.exit("Give a colour, e.g. g510 poweron blue")
    try:
        rgb = config.parse_color(args[0])
    except config.ColorError as exc:
        sys.exit(str(exc))
    guard(lambda: control.set_power_on(rgb))
    print(f"power-on colour -> {config.format_color(rgb)}")


def cmd_mkeys(args):
    for name in args:
        if name.lower() not in device.MKEY_BITS and name.lower() != "off":
            sys.exit(f"Unknown M-key {name!r}. Use: m1 m2 m3 mr")
    names = [a for a in args if a.lower() != "off"]
    guard(lambda: control.set_mkeys(names))
    print(f"M-key LEDs -> {' '.join(args) if args else 'off'}")


def cmd_watch(args):
    raw = "--raw" in args
    settings = config.load()
    via = "agent" if control.daemon_running() else "direct"
    print(f"Watching for G-key presses ({via}). Ctrl-C to stop.")
    try:
        for message in control.watch_gkeys():
            event = message.get("event")
            if event == "ping":
                continue
            name = message.get("key", "?")
            if event == "press":
                binding = settings.get("bindings", {}).get(name)
                print(f"  press   {name:4} -> {actions.describe(binding)}")
                if raw and message.get("raw"):
                    print(f"          raw: {message['raw']}")
            elif event == "release":
                print(f"  release {name}")
    except control.ControlError as exc:
        sys.exit(str(exc))
    except KeyboardInterrupt:
        print("\nstopped")


def reload_agent():
    """Tell a running agent to re-read the config file, if one is there."""
    try:
        if control.daemon_running():
            ipc.request({"cmd": "reload"})
    except OSError:
        pass


def cmd_lcd(args):
    """Change what the LCD shows, and make the change stick.

    The agent redraws the display every second, so a one-shot draw would be
    overwritten immediately. Picking a screen updates the config instead, and
    explicit text or a clear switches the auto-refresh off so it stays put.
    """
    if not args:
        sys.exit("Give a screen name, 'text', or 'clear'. See g510 --help")
    settings = config.load()
    lcd = settings.setdefault("lcd", {})
    if args[0] == "clear":
        lcd["enabled"] = False
        save_config(settings)
        reload_agent()
        guard(control.clear_lcd)
        print("LCD cleared (auto-refresh off; re-enable with g510 lcd status)")
    elif args[0] == "text":
        if len(args) < 2:
            sys.exit('Give up to 3 lines, e.g. g510 lcd text "hello" "there"')
        lcd["enabled"] = False
        save_config(settings)
        reload_agent()
        guard(lambda: control.show_text(list(args[1:4])))
        print("LCD updated (auto-refresh off; re-enable with g510 lcd status)")
    elif args[0] in screens.SCREENS:
        lcd["screen"] = args[0]
        lcd["enabled"] = True
        save_config(settings)
        reload_agent()
        guard(lambda: control.show_screen(args[0]))
        print(f"LCD -> {args[0]}")
    else:
        sys.exit(f"Unknown screen {args[0]!r}. "
                 f"Try: {', '.join(screens.SCREENS)}, text, clear")


def cmd_bindings(_args):
    settings = config.load()
    bindings = settings.get("bindings", {})
    for index in range(1, device.GKEY_COUNT + 1):
        name = f"G{index}"
        print(f"  {name:4} {actions.describe(bindings.get(name))}")


def cmd_bind(args):
    if len(args) < 2:
        sys.exit('Usage: g510 bind G1 app Safari | keys cmd+c | '
                 'shell "say hi" | text "hello" | none')
    key = args[0].upper()
    if not (key.startswith("G") and key[1:].isdigit()
            and 1 <= int(key[1:]) <= device.GKEY_COUNT):
        sys.exit(f"{args[0]!r} is not a G-key (G1..G{device.GKEY_COUNT})")
    kind = args[1].lower()
    settings = config.load()
    bindings = settings.setdefault("bindings", {})
    if kind == "none":
        bindings.pop(key, None)
        save_config(settings)
        print(f"{key} cleared")
        return
    value = " ".join(args[2:])
    if not value:
        sys.exit(f"Give something to bind, e.g. g510 bind {key} app Safari")
    shapes = {"app": ("name", value), "keys": ("keys", value),
              "shell": ("command", value), "text": ("text", value)}
    if kind not in shapes:
        sys.exit(f"Unknown type {kind!r}. Use: app, keys, shell, text, none")
    if kind == "keys":
        try:
            actions.parse_chord(value)
        except actions.ActionError as exc:
            sys.exit(str(exc))
    field, content = shapes[kind]
    bindings[key] = {"type": kind, field: content}
    save_config(settings)
    reload_agent()
    print(f"{key} -> {actions.describe(bindings[key])}")


def cmd_brightness(args):
    """Show or set backlight brightness.

    The G510 has no brightness register, so this scales the chosen colour.
    """
    settings = config.load()
    if not args:
        print(f"{settings.get('brightness', 100)}%")
        return
    raw = args[0].rstrip("%")
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"Brightness must be a number from 0 to 100, not {args[0]!r}")
    if not 0 <= value <= 100:
        sys.exit("Brightness must be between 0 and 100")
    settings["brightness"] = value
    save_config(settings)
    guard(lambda: control.set_backlight(config.effective_color(settings)))
    reload_agent()
    print(f"brightness -> {value}%  "
          f"({config.format_color(config.effective_color(settings))})")


def cmd_record(args):
    """Record a keystroke sequence and bind it to a G-key."""
    if not args:
        sys.exit("Usage: g510 record G5 [macro name]")
    key = args[0].upper()
    if not (key.startswith("G") and key[1:].isdigit()
            and 1 <= int(key[1:]) <= device.GKEY_COUNT):
        sys.exit(f"{args[0]!r} is not a G-key (G1..G{device.GKEY_COUNT})")
    if not actions.can_post_events():
        sys.exit("Recording needs Accessibility permission - run: g510 permissions")
    name = " ".join(args[1:]) or key.lower()

    print(f"Recording a macro for {key}.")
    print("  Type the sequence, then press Escape to finish.")
    print("  Keys still reach whatever app is focused, so switch away first")
    print("  if you do not want them landing somewhere.\n")
    try:
        steps = recorder.record(
            on_step=lambda chord, n: print(f"  {n:2}. {chord}"))
    except recorder.RecordingError as exc:
        sys.exit(str(exc))
    if not steps:
        print("\nNothing recorded; binding unchanged.")
        return
    settings = config.load()
    settings.setdefault("macros", {})[name] = steps
    settings.setdefault("bindings", {})[key] = {"type": "macro", "name": name}
    save_config(settings)
    reload_agent()
    print(f"\n{key} -> macro {name!r} ({len(steps)} steps)")


def cmd_macros(args):
    settings = config.load()
    macros = settings.get("macros") or {}
    if args and args[0] == "delete":
        if len(args) < 2:
            sys.exit("Usage: g510 macros delete <name>")
        name = " ".join(args[1:])
        if macros.pop(name, None) is None:
            sys.exit(f"No macro called {name!r}")
        for key, binding in list(settings.get("bindings", {}).items()):
            if binding.get("type") == "macro" and binding.get("name") == name:
                settings["bindings"].pop(key)
        save_config(settings)
        reload_agent()
        print(f"deleted macro {name!r}")
        return
    if not macros:
        print("  no macros recorded - try: g510 record G5")
        return
    for name, steps in macros.items():
        print(f"  {name:16} {recorder.summarise(steps)}")


def cmd_profile(args):
    """Per-app binding overrides: g510 profile "Final Cut Pro" G1 keys cmd+z"""
    settings = config.load()
    profiles = settings.setdefault("app_profiles", {})
    if not args:
        if not profiles:
            print("  no per-app profiles - try: "
                  'g510 profile "Safari" G1 keys cmd+t')
            return
        for app, bindings in profiles.items():
            print(f"  {app}")
            for key in sorted(bindings, key=_gkey_order):
                print(f"    {key:4} {actions.describe(bindings[key])}")
        return
    if len(args) < 3:
        sys.exit('Usage: g510 profile "App Name" G1 app|keys|shell|text|none <value>')
    app, key, kind = args[0], args[1].upper(), args[2].lower()
    if not (key.startswith("G") and key[1:].isdigit()
            and 1 <= int(key[1:]) <= device.GKEY_COUNT):
        sys.exit(f"{args[1]!r} is not a G-key (G1..G{device.GKEY_COUNT})")
    bindings = profiles.setdefault(app, {})
    if kind == "none":
        bindings.pop(key, None)
        if not bindings:
            profiles.pop(app, None)
        save_config(settings)
        reload_agent()
        print(f"{app}: {key} cleared")
        return
    value = " ".join(args[3:])
    shapes = {"app": "name", "keys": "keys", "shell": "command", "text": "text"}
    if kind not in shapes:
        sys.exit(f"Unknown type {kind!r}. Use: app, keys, shell, text, none")
    if not value:
        sys.exit(f"Give something to bind for {key}")
    if kind == "keys":
        try:
            actions.parse_chord(value)
        except actions.ActionError as exc:
            sys.exit(str(exc))
    bindings[key] = {"type": kind, shapes[kind]: value}
    save_config(settings)
    reload_agent()
    print(f"{app}: {key} -> {actions.describe(bindings[key])}")


def cmd_bank(args):
    """Show or switch the active binding bank (M1, M2, M3)."""
    settings = config.load()
    if not args:
        active = settings.get("active_bank", "1")
        for name in config.BANKS:
            count = len(settings.get("banks", {}).get(name, {}))
            mark = " <- active" if name == active else ""
            print(f"  M{name}  {count} binding" + ("s" if count != 1 else "")
                  + mark)
        return
    name = args[0].lstrip("mM")
    if name not in config.BANKS:
        sys.exit(f"Bank must be 1, 2 or 3, not {args[0]!r}")
    if control.daemon_running():
        try:
            reply = ipc.request({"cmd": "set_bank", "bank": name})
        except OSError as exc:
            sys.exit(f"The agent is not responding ({exc}). Try: g510 start")
        if not reply.get("ok"):
            sys.exit(reply.get("error", "agent refused"))
    else:
        config.set_bank(settings, name)
        save_config(settings)
        guard(lambda: control.set_mkeys([f"m{name}"]))
    print(f"active bank -> M{name}")


def cmd_switch(args):
    """Show or set what the joystick (game-mode) switch does."""
    settings = config.load()
    current = settings.get("game_switch") or {}
    if not args:
        print(f"  engaged     {current.get('on') or 'nothing'}")
        print(f"  released    {current.get('off') or 'nothing'}")
        print("\n  set with: g510 switch on bank:3   |   off bank:1")
        print("  values:   bank:1-3, screen:<name>, color:<colour>, none")
        return
    if len(args) < 2:
        sys.exit("Usage: g510 switch on|off <action>")
    position = args[0].lower()
    if position not in ("on", "off"):
        sys.exit("First argument must be 'on' or 'off'")
    value = args[1]
    if value.lower() == "none":
        current.pop(position, None)
    else:
        kind, _, rest = value.partition(":")
        if kind == "bank" and rest not in config.BANKS:
            sys.exit(f"Bank must be one of {', '.join(config.BANKS)}")
        if kind == "screen" and rest not in screens.SCREENS:
            sys.exit(f"Unknown screen {rest!r}. Try: {', '.join(screens.SCREENS)}")
        if kind == "color":
            try:
                config.parse_color(rest)
            except config.ColorError as exc:
                sys.exit(str(exc))
        if kind not in ("bank", "screen", "color"):
            sys.exit("Action must be bank:N, screen:<name>, color:<colour> or none")
        current[position] = value
    settings["game_switch"] = current
    save_config(settings)
    reload_agent()
    print(f"switch {position} -> {current.get(position) or 'nothing'}")


def cmd_printer(args):
    """Show or set the 3D printer the display reads from."""
    import printer

    settings = config.load()
    block = settings.setdefault("printer", {})
    if not args:
        host = block.get("host") or "not set"
        print(f"  host     {host}:{block.get('port', 9999)}")
        print(f"  enabled  {'yes' if block.get('enabled') else 'no'}")
        reading = printer.status(force=True)
        if reading:
            print(f"  state    {printer.describe_state(reading)}"
                  f"  {reading['progress']}%")
            if reading["file"]:
                print(f"  file     {reading['file']}")
            print(f"  temps    nozzle {reading['nozzle']:.0f}"
                  f"  bed {reading['bed']:.0f}")
        elif printer.last_error():
            print(f"  error    {printer.last_error()}")
        print("\n  set with: g510 printer 192.168.1.50   |   g510 printer off")
        return
    if args[0].lower() in ("off", "none", "disable"):
        block["enabled"] = False
        save_config(settings)
        reload_agent()
        print("printer screen disabled")
        return
    host, _, port = args[0].partition(":")
    block["host"] = host
    block["enabled"] = True
    if port.isdigit():
        block["port"] = int(port)
    save_config(settings)
    reload_agent()
    reading = printer.status(force=True)
    if reading:
        print(f"printer -> {host}  ({reading['model']}, "
              f"{printer.describe_state(reading)})")
    else:
        print(f"printer -> {host}, but it did not answer: "
              f"{printer.last_error()}")


def cmd_claude(args):
    """Choose which usage windows the Claude screen shows."""
    settings = config.load()
    block = settings.setdefault("claude", {})
    if not args:
        print(f"  rows   {', '.join(block.get('rows') or [])}")
        print(f"  stale  after {block.get('stale_after_seconds', 600)}s")
        print("\n  choose any two: " + ", ".join(screens.CLAUDE_ROWS))
        print("  e.g. g510 claude five_hour context")
        return
    wanted = [a for a in args if a in screens.CLAUDE_ROWS]
    unknown = [a for a in args if a not in screens.CLAUDE_ROWS]
    if unknown:
        sys.exit(f"Unknown row(s) {', '.join(unknown)}. "
                 f"Choose from: {', '.join(screens.CLAUDE_ROWS)}")
    block["rows"] = wanted[:2]
    save_config(settings)
    reload_agent()
    print(f"claude screen -> {', '.join(block['rows'])}")


def cmd_next(_args):
    """Advance the LCD to the next screen in the cycle."""
    if control.daemon_running():
        try:
            reply = ipc.request({"cmd": "next_screen"})
        except OSError as exc:
            sys.exit(f"The agent is not responding ({exc}). Try: g510 start")
        if reply.get("ok"):
            print(f"LCD -> {reply.get('screen')}")
            return
        sys.exit(reply.get("error", "agent refused"))
    settings = config.load()
    lcd = settings.setdefault("lcd", {})
    cycle = [n for n in lcd.get("cycle") or [] if n in screens.SCREENS] or list(screens.SCREENS)
    try:
        index = cycle.index(lcd.get("screen", cycle[0]))
    except ValueError:
        index = -1
    choice = cycle[(index + 1) % len(cycle)]
    lcd["screen"], lcd["enabled"] = choice, True
    save_config(settings)
    guard(lambda: control.show_screen(choice))
    print(f"LCD -> {choice}")


def cmd_permissions(_args):
    """Report and request the macOS permissions the bindings need."""
    reading = actions.can_read_input()
    posting = actions.can_post_events()
    print(f"  Input Monitoring  {'granted' if reading else 'NOT granted'}"
          "   (reading G-key presses)")
    print(f"  Accessibility     {'granted' if posting else 'NOT granted'}"
          "   (keys/text bindings)")
    if posting and reading:
        print("\nBoth granted - every binding type will work.")
        return
    if not posting:
        print("\nWithout Accessibility, 'keys' and 'text' bindings do nothing.")
        print("'app' and 'shell' bindings work regardless.")
        print("\nAsking macOS now - approve the prompt, then add this binary")
        print("under System Settings > Privacy & Security > Accessibility:")
        print(f"  {PYTHON}")
        actions.request_post_access()
        print("\nAfter approving, restart the agent:  g510 stop && g510 start")


def cmd_config(_args):
    created = config.ensure_exists()
    print(config.CONFIG_PATH + ("  (created)" if created else ""))


# -- launch agent ----------------------------------------------------------

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{app_dir}/daemon.py</string>
  </array>
  <key>WorkingDirectory</key><string>{app_dir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""

LOG_PATH = os.path.expanduser("~/Library/Logs/g510.log")


def agent_loaded():
    result = subprocess.run(["launchctl", "list", LABEL],
                            capture_output=True, text=True)
    return result.returncode == 0


def cmd_start(_args):
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "w") as handle:
        handle.write(PLIST.format(label=LABEL, python=PYTHON,
                                  app_dir=APP_DIR, log=LOG_PATH))
    subprocess.run(["launchctl", "unload", PLIST_PATH],
                   capture_output=True)
    result = subprocess.run(["launchctl", "load", "-w", PLIST_PATH],
                            capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"launchctl load failed: {result.stderr.strip()}")
    print(f"agent loaded; it will start at login\n  log: {LOG_PATH}")


def cmd_stop(_args):
    if not os.path.exists(PLIST_PATH):
        sys.exit("Agent was never installed (nothing to stop)")
    subprocess.run(["launchctl", "unload", "-w", PLIST_PATH],
                   capture_output=True)
    print("agent stopped and disabled")


def cmd_status(_args):
    print(f"  agent:  {'running' if agent_loaded() else 'not loaded'}")
    print(f"  plist:  {PLIST_PATH}")
    print(f"  log:    {LOG_PATH}")
    if os.path.exists(LOG_PATH):
        print("  recent:")
        with open(LOG_PATH) as handle:
            for line in collections.deque(handle, maxlen=6):
                print("    " + line.rstrip())


def cmd_run(_args):
    import daemon
    daemon.main()


def cmd_gui(_args):
    import gui
    gui.main()


COMMANDS = {
    "info": cmd_info, "color": cmd_color, "colour": cmd_color,
    "poweron": cmd_poweron, "mkeys": cmd_mkeys, "watch": cmd_watch,
    "lcd": cmd_lcd, "bindings": cmd_bindings, "bind": cmd_bind,
    "config": cmd_config, "permissions": cmd_permissions,
    "brightness": cmd_brightness, "record": cmd_record, "macros": cmd_macros,
    "profile": cmd_profile, "profiles": cmd_profile, "next": cmd_next,
    "bank": cmd_bank, "switch": cmd_switch,
    "printer": cmd_printer, "claude": cmd_claude, "start": cmd_start, "stop": cmd_stop,
    "status": cmd_status, "run": cmd_run, "gui": cmd_gui,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    command = COMMANDS.get(sys.argv[1])
    if not command:
        sys.exit(f"Unknown command {sys.argv[1]!r}. Try: g510 --help")
    command(sys.argv[2:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
