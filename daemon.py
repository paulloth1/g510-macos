"""Background agent: owns the keyboard, dispatches G-keys, drives the LCD.

hidapi takes the HID device exclusively, so while this runs it holds the only
handle. The CLI and GUI reach the keyboard by asking this process over a unix
socket (see ipc.py and control.py).

The keyboard lives on a dock, so it can vanish and come back at any time. A
missing device is treated as a normal state, not an error.
"""
import queue
import signal
import sys
import threading
import time

import actions
import config
import device
import ipc
import recorder
import screens

RECONNECT_DELAY = 2.0
POLL_INTERVAL = 0.004
APP_CHECK_INTERVAL = 0.75


class Daemon:
    def __init__(self):
        self.config = config.load()
        self.keyboard = None
        self.running = True
        self.state = {}
        self.pressed = set()
        self.lcd_pressed = set()
        self.mode_pressed = set()
        self.record_state = None       # None | "await_key" | "recording"
        self.record_target = None
        self.recorder = None
        self.last_lcd = 0.0
        self.last_app_check = 0.0
        self.current_app = None
        self.base_color = config.effective_color(self.config)
        self.lock = threading.Lock()      # guards every device touch
        self.watchers = []
        self.watchers_lock = threading.Lock()
        self.server = ipc.Server(self.handle_request)

    # -- lifecycle ---------------------------------------------------------

    def stop(self, *_args):
        self.running = False

    def connect(self):
        try:
            self.keyboard = device.G510()
            self.keyboard.set_nonblocking(True)
        except (device.DeviceNotFound, OSError):
            self.keyboard = None
            return False
        try:
            self.keyboard.silence_gkey_scancodes()
        except OSError as exc:
            log(f"could not silence G-key scancodes: {exc}")
        if self.config.get("restore_backlight_on_start", True):
            self.keyboard.set_backlight(self.base_color)
        self.show_bank_led()
        self.state = {}
        log("connected to G510")
        return True

    def drop(self, reason):
        log(f"lost keyboard ({reason}); waiting for it to come back")
        try:
            if self.keyboard:
                self.keyboard.close()
        except Exception:
            pass
        self.keyboard = None
        self.pressed = set()
        self.lcd_pressed = set()

    # -- control socket ----------------------------------------------------

    def handle_request(self, payload):
        """Yield reply dicts for one client request."""
        command = payload.get("cmd")
        if command == "watch":
            yield from self.stream_gkeys()
            return
        try:
            yield self.run_command(command, payload)
        except device.DeviceNotFound:
            yield {"ok": False, "error": "keyboard not connected"}
        except (OSError, ValueError, KeyError) as exc:
            yield {"ok": False, "error": str(exc)}

    def run_command(self, command, payload):
        with self.lock:
            if self.keyboard is None:
                return {"ok": False, "error": "keyboard not connected"}
            if command == "get_backlight":
                return {"ok": True, "rgb": list(self.keyboard.get_backlight())}
            if command == "set_backlight":
                rgb = tuple(payload["rgb"])
                self.keyboard.set_backlight(rgb)
                self.base_color = rgb
                return {"ok": True}
            if command == "get_config":
                return {"ok": True, "active_bank":
                        str(self.config.get("active_bank", "1"))}
            if command == "set_power_on":
                self.keyboard.set_power_on_backlight(tuple(payload["rgb"]))
                return {"ok": True}
            if command == "set_mkeys":
                self.keyboard.set_mkeys(payload.get("names", []))
                return {"ok": True}
            if command == "send_lcd":
                self.keyboard._dev.write(bytes.fromhex(payload["frame"]))
                return {"ok": True}
            if command == "silence_gkeys":
                self.keyboard.silence_gkey_scancodes()
                return {"ok": True}
            if command == "clear_lcd":
                self.keyboard.clear_lcd()
                return {"ok": True}
            if command == "info":
                return {"ok": True,
                        "manufacturer": self.keyboard._dev.get_manufacturer_string(),
                        "product": self.keyboard._dev.get_product_string()}
            if command == "reload":
                self.config = config.load()
                self.base_color = config.effective_color(self.config)
                if self.config.get("restore_backlight_on_start", True):
                    self.keyboard.set_backlight(self.active_color())
                self.show_bank_led()
                return {"ok": True}
            if command == "next_screen":
                return {"ok": True, "screen": self.advance_screen(1)}
            if command == "set_bank":
                return {"ok": True, "bank": self.select_bank(payload["bank"])}
            return {"ok": False, "error": f"unknown command {command!r}"}

    def stream_gkeys(self):
        """Feed G-key events to one watching client until it disconnects."""
        events = queue.Queue()
        with self.watchers_lock:
            self.watchers.append(events)
        try:
            while self.running:
                try:
                    yield events.get(timeout=1.0)
                except queue.Empty:
                    yield {"event": "ping"}
        finally:
            with self.watchers_lock:
                if events in self.watchers:
                    self.watchers.remove(events)

    def broadcast(self, message):
        with self.watchers_lock:
            for events in self.watchers:
                events.put(message)

    # -- work --------------------------------------------------------------

    def handle_gkeys(self):
        with self.lock:
            data = self.keyboard.read_raw()
        if data is None:
            return
        # Raw reports go out so unknown buttons can be mapped, but never the
        # boot-keyboard report - that is the user's actual typing.
        if not device.G510.carries_keystrokes(data):
            self.broadcast({"event": "raw", "raw": data.hex()})
        keys = device.G510.decode_gkeys(data)
        if keys is None:
            return
        for name in sorted(keys - self.pressed):
            if self.record_state == "await_key":
                self.start_recording(name)
            else:
                self.fire(name)
            self.broadcast({"event": "press", "key": name, "raw": data.hex()})
        for name in sorted(self.pressed - keys):
            self.broadcast({"event": "release", "key": name})
        self.pressed = keys

        mode_keys = device.G510.decode_mode_keys(data)
        if mode_keys is not None:
            for name in sorted(mode_keys - self.mode_pressed):
                self.fire_mode_key(name)
            for name in sorted(self.mode_pressed - mode_keys):
                if name == "GAME":
                    self.fire_game_switch(False)
            self.mode_pressed = mode_keys

        lcd_keys = device.G510.decode_lcd_keys(data)
        if lcd_keys is not None:
            for name in sorted(lcd_keys - self.lcd_pressed):
                self.fire_lcd_key(name)
                self.broadcast({"event": "press", "key": name})
            self.lcd_pressed = lcd_keys

    @property
    def bindings(self):
        """Base bindings, overlaid with any profile for the frontmost app."""
        merged = dict(self.config.get("bindings", {}))
        profile = (self.config.get("app_profiles") or {}).get(self.current_app)
        if isinstance(profile, dict):
            for key, value in profile.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
        return merged

    def active_color(self):
        """Backlight colour for the frontmost app, brightness applied."""
        wanted = (self.config.get("app_colors") or {}).get(self.current_app)
        if not wanted:
            return self.base_color
        try:
            return config.apply_brightness(
                config.parse_color(wanted), self.config.get("brightness", 100))
        except config.ColorError:
            return self.base_color

    def mutate(self, apply):
        """Re-read the config, apply a change, write it back.

        The CLI and GUI write this file too. Saving our own long-held copy
        would silently revert whatever they changed since we last read it.
        """
        fresh = config.load()
        apply(fresh)
        config.save(fresh)
        self.config = fresh
        return fresh

    def select_bank(self, name):
        """Switch the active binding bank and light its M-key."""
        self.mutate(lambda settings: config.set_bank(settings, name))
        self.show_bank_led()
        log(f"bank -> M{name}")
        return str(name)

    def show_bank_led(self):
        active = str(self.config.get("active_bank", "1"))
        if self.keyboard is not None:
            self.keyboard.set_mkeys([f"m{active}"])

    def advance_screen(self, step=1):
        """Move to the next screen in the configured cycle."""
        lcd = self.config.setdefault("lcd", {})
        cycle = [name for name in lcd.get("cycle") or [] if name in screens.SCREENS]
        if not cycle:
            cycle = list(screens.SCREENS)
        if not lcd.get("enabled", True):
            lcd["enabled"] = True
            current = cycle[0]
        else:
            try:
                index = cycle.index(lcd.get("screen", cycle[0]))
            except ValueError:
                index = -1
            current = cycle[(index + step) % len(cycle)]
        def apply(settings):
            target = settings.setdefault("lcd", {})
            target["screen"], target["enabled"] = current, True

        self.mutate(apply)
        self.last_lcd = 0.0
        log(f"LCD -> {current}")
        return current

    def fire_mode_key(self, name):
        """M1/M2/M3 pick a bank, MR drives recording, GAME is the switch."""
        if name in ("M1", "M2", "M3"):
            if self.record_state:
                return
            self.select_bank(name[1])
        elif name == "MR":
            self.toggle_recording()
        elif name == "GAME":
            self.fire_game_switch(True)

    def fire_game_switch(self, engaged):
        """Run whatever the joystick switch is configured to do."""
        action = (self.config.get("game_switch") or {}).get(
            "on" if engaged else "off")
        log(f"game switch {'on' if engaged else 'off'}")
        if not action:
            return
        if isinstance(action, dict):
            try:
                actions.dispatch(action, self.config.get("macros"))
            except actions.ActionError as exc:
                log(f"  game switch failed: {exc}")
        elif action.startswith("bank:"):
            self.select_bank(action.split(":", 1)[1])
        elif action.startswith("screen:"):
            wanted = action.split(":", 1)[1]
            if wanted in screens.SCREENS:
                self.mutate(lambda s: s.setdefault("lcd", {}).update(
                    screen=wanted, enabled=True))
                self.last_lcd = 0.0
        elif action.startswith("color:"):
            try:
                rgb = config.apply_brightness(
                    config.parse_color(action.split(":", 1)[1]),
                    self.config.get("brightness", 100))
                with self.lock:
                    self.keyboard.set_backlight(rgb)
            except config.ColorError:
                pass

    def toggle_recording(self):
        """MR: arm, then record onto the next G-key, then finish."""
        if self.record_state is None:
            if not actions.can_post_events():
                log("MR pressed but Accessibility is not granted")
                return
            self.record_state = "await_key"
            self.record_target = None
            self.last_lcd = 0.0
            log("MR -> waiting for a G-key")
        elif self.record_state == "await_key":
            self.record_state = None
            self.last_lcd = 0.0
            log("MR -> recording cancelled")
        else:
            self.finish_recording()

    def start_recording(self, key):
        self.record_target = key
        self.record_state = "recording"
        self.last_lcd = 0.0
        log(f"recording macro onto {key}")
        self.recorder = recorder.Recorder()
        thread = threading.Thread(target=self._record_thread, daemon=True)
        thread.start()

    def _record_thread(self):
        try:
            self.recorder.run(timeout=300.0)
        except recorder.RecordingError as exc:
            log(f"recording failed: {exc}")
        finally:
            if self.record_state == "recording":
                self.finish_recording()

    def finish_recording(self):
        steps = list(self.recorder.steps) if self.recorder else []
        key, self.record_target = self.record_target, None
        self.record_state = None
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
        self.last_lcd = 0.0
        if not steps or not key:
            log("recording discarded (nothing captured)")
            return
        name = f"{key.lower()}-m{self.config.get('active_bank', '1')}"

        def apply(settings):
            settings.setdefault("macros", {})[name] = steps
            settings.setdefault("bindings", {})[key] = {
                "type": "macro", "name": name}

        self.mutate(apply)
        log(f"{key} -> macro {name!r} ({len(steps)} steps)")

    def fire_lcd_key(self, name):
        """Run whatever the display key is configured to do."""
        action = (self.config.get("lcd_keys") or {}).get(name)
        if not action:
            return
        if isinstance(action, dict):
            try:
                actions.dispatch(action, self.config.get("macros"))
            except actions.ActionError as exc:
                log(f"{name} failed: {exc}")
            return
        if action == "next":
            self.advance_screen(1)
        elif action == "prev":
            self.advance_screen(-1)
        elif action == "off":
            self.mutate(lambda s: s.setdefault("lcd", {}).update(enabled=False))
            with self.lock:
                self.keyboard.clear_lcd()
            log(f"{name} -> LCD off")
        elif action.startswith("screen:"):
            wanted = action.split(":", 1)[1]
            if wanted in screens.SCREENS:
                self.mutate(lambda s: s.setdefault("lcd", {}).update(
                    screen=wanted, enabled=True))
                self.last_lcd = 0.0
                log(f"{name} -> LCD {wanted}")

    def fire(self, name):
        binding = self.bindings.get(name)
        self.state["last_gkey"] = name
        self.state["last_action"] = actions.describe(binding)
        if not binding:
            log(f"{name} pressed (unbound)")
            return
        log(f"{name} -> {actions.describe(binding)}")
        try:
            actions.dispatch(binding, self.config.get("macros"))
        except actions.ActionError as exc:
            log(f"  binding failed: {exc}")

    def refresh_lcd(self, now):
        lcd_config = self.config.get("lcd", {})
        if not lcd_config.get("enabled", True):
            return
        if now - self.last_lcd < float(lcd_config.get("refresh_seconds", 1.0)):
            return
        self.last_lcd = now
        if self.record_state:
            self.state["record_state"] = self.record_state
            self.state["record_target"] = self.record_target
            self.state["record_steps"] = (
                len(self.recorder.steps) if self.recorder else 0)
            pixels = screens.render("record", self.state)
        else:
            self.state.pop("record_state", None)
            pixels = screens.render(lcd_config.get("screen", "status"), self.state)
        with self.lock:
            self.keyboard.send_lcd(pixels)

    def refresh_app_color(self, now):
        """Track the frontmost app: both colours and profiles depend on it."""
        if now - self.last_app_check < APP_CHECK_INTERVAL:
            return
        self.last_app_check = now
        name = screens.frontmost_app()
        if name == self.current_app:
            return
        self.current_app = name
        with self.lock:
            self.keyboard.set_backlight(self.active_color())

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log("g510 daemon starting")
        self.server.start()
        log(f"control socket at {ipc.SOCKET_PATH}")
        while self.running:
            if self.keyboard is None:
                if not self.connect():
                    time.sleep(RECONNECT_DELAY)
                    continue
            try:
                now = time.time()
                self.handle_gkeys()
                self.refresh_lcd(now)
                self.refresh_app_color(now)
            except (OSError, ValueError) as exc:
                self.drop(exc)
                time.sleep(RECONNECT_DELAY)
                continue
            time.sleep(POLL_INTERVAL)
        self.shutdown()

    def shutdown(self):
        log("g510 daemon stopping")
        self.server.stop()
        if self.keyboard:
            try:
                self.keyboard.clear_lcd()
            except Exception:
                pass
            try:
                self.keyboard.close()
            except Exception:
                pass


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def main():
    Daemon().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
