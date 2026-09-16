"""A menu bar app for the G510, built on AppKit.

Deliberately a status item rather than a window: the things you change often
(backlight colour, LCD screen, whether the agent runs) are one click each, and
anything deeper opens the JSON config.
"""
import subprocess
import sys

import objc
from AppKit import (NSAlert, NSApplication, NSApplicationActivationPolicyAccessory,
                    NSApplicationActivationPolicyRegular, NSColor,
                    NSColorPanel, NSImage, NSMenu, NSMenuItem, NSStatusBar,
                    NSVariableStatusItemLength, NSWorkspace)
from Foundation import NSObject

import actions
import cli
import config
import control
import device
import ipc
import screens

MENU_COLORS = ["white", "red", "orange", "yellow", "green",
               "cyan", "blue", "purple", "pink", "off"]


class G510Menu(NSObject):

    def init(self):
        self = objc.super(G510Menu, self).init()
        if self is None:
            return None
        self.config = config.load()
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        # macOS remembers a status item as hidden across launches, so say so
        # explicitly rather than trusting the default.
        self.status_item.setVisible_(True)
        self.set_icon(connected=True)
        self.rebuild()
        return self

    @objc.python_method
    def set_icon(self, connected=True):
        """A keyboard glyph reads better in a crowded menu bar than text."""
        button = self.status_item.button()
        # Symbol availability varies by macOS release, so fall through to
        # whatever this machine actually has, and to text as a last resort.
        candidates = (["keyboard"] if connected
                      else ["keyboard.badge.exclamationmark", "keyboard"])
        for symbol in candidates:
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                symbol, "G510")
            if image is not None:
                image.setTemplate_(True)
                button.setImage_(image)
                button.setTitle_("" if connected else " ?")
                break
        else:
            button.setImage_(None)
            button.setTitle_("G510" if connected else "G510?")
        button.setToolTip_("G510 keyboard"
                           if connected else "G510 keyboard not found")

    # -- helpers -----------------------------------------------------------

    @objc.python_method
    def item(self, title, selector, target=None, state=False, enabled=True):
        entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, selector, "")
        entry.setTarget_(target or self)
        entry.setState_(1 if state else 0)
        entry.setEnabled_(enabled)
        return entry

    @objc.python_method
    def attempt(self, work):
        """Run a control-layer call, flagging the status item if it fails."""
        try:
            work()
            self.set_icon(connected=True)
            return True
        except control.ControlError:
            self.set_icon(connected=False)
            return False

    @objc.python_method
    def mutate(self, apply):
        """Re-read, change, write back.

        The menu's cached config is only refreshed when the menu is rebuilt,
        so it can be minutes old. Writing it wholesale would revert a bank the
        keyboard switched or a macro MR recorded in the meantime.
        """
        fresh = config.load()
        apply(fresh)
        if not config.save(fresh):
            return fresh
        self.config = fresh
        if ipc.is_running():
            try:
                ipc.request({"cmd": "reload"})
            except OSError:
                pass
        return fresh

    # -- menu construction -------------------------------------------------

    @objc.python_method
    def rebuild(self):
        self.config = config.load()
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)

        connected = control.present()
        header = self.item(
            "Keyboard connected" if connected else "Keyboard not found",
            None, enabled=False)
        menu.addItem_(header)
        menu.addItem_(NSMenuItem.separatorItem())

        current = config.format_color(
            config.parse_color(self.config.get("backlight", "#ffffff")))
        backlight = NSMenu.alloc().init()
        backlight.setAutoenablesItems_(False)
        for name in MENU_COLORS:
            swatch = config.format_color(config.NAMED_COLORS[name])
            entry = self.item(f"{name.title()}  {swatch}",
                              b"pickColor:", state=(swatch == current))
            entry.setRepresentedObject_(name)
            backlight.addItem_(entry)
        backlight.addItem_(NSMenuItem.separatorItem())
        backlight.addItem_(self.item("Custom…", b"openColorPanel:"))
        backlight_item = self.item(f"Backlight  {current}", None)
        menu.addItem_(backlight_item)
        menu.setSubmenu_forItem_(backlight, backlight_item)

        lcd_config = self.config.get("lcd", {})
        active_screen = lcd_config.get("screen", "status")
        lcd_enabled = lcd_config.get("enabled", True)
        lcd = NSMenu.alloc().init()
        lcd.setAutoenablesItems_(False)
        for name in screens.SCREENS:
            entry = self.item(name.title(), b"pickScreen:",
                              state=(lcd_enabled and name == active_screen))
            entry.setRepresentedObject_(name)
            lcd.addItem_(entry)
        lcd.addItem_(NSMenuItem.separatorItem())
        lcd.addItem_(self.item("Off", b"disableLcd:", state=not lcd_enabled))
        lcd_item = self.item(
            f"LCD  {active_screen if lcd_enabled else 'off'}", None)
        menu.addItem_(lcd_item)
        menu.setSubmenu_forItem_(lcd, lcd_item)

        bindings = self.config.get("bindings", {})
        keys = NSMenu.alloc().init()
        keys.setAutoenablesItems_(False)
        for index in range(1, device.GKEY_COUNT + 1):
            name = f"G{index}"
            keys.addItem_(self.item(
                f"{name:4} {actions.describe(bindings.get(name))}",
                b"editConfig:"))
        keys.addItem_(NSMenuItem.separatorItem())
        keys.addItem_(self.item("Edit bindings…", b"editConfig:"))
        keys_item = self.item(
            f"G-keys  {len(bindings)} bound", None)
        menu.addItem_(keys_item)
        menu.setSubmenu_forItem_(keys, keys_item)

        menu.addItem_(NSMenuItem.separatorItem())
        running = cli.agent_loaded()
        menu.addItem_(self.item(
            "Background agent", b"toggleAgent:", state=running))
        menu.addItem_(self.item("Open config file", b"editConfig:"))
        menu.addItem_(self.item("Refresh", b"refresh:"))
        menu.addItem_(NSMenuItem.separatorItem())
        menu.addItem_(self.item("Open G510 window", b"showWindow:"))
        menu.addItem_(self.item("Quit", b"quit:"))
        self.status_item.setMenu_(menu)

    # -- actions -----------------------------------------------------------

    @objc.IBAction
    def pickColor_(self, sender):
        name = sender.representedObject()
        rgb = config.NAMED_COLORS[name]
        self.apply_color(rgb)

    @objc.IBAction
    def openColorPanel_(self, _sender):
        panel = NSColorPanel.sharedColorPanel()
        panel.setTarget_(self)
        panel.setAction_(b"colorPanelChanged:")
        panel.setContinuous_(True)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        panel.makeKeyAndOrderFront_(None)

    @objc.IBAction
    def colorPanelChanged_(self, sender):
        color = sender.color().colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
        if color is None:
            return
        rgb = (int(color.redComponent() * 255),
               int(color.greenComponent() * 255),
               int(color.blueComponent() * 255))
        self.apply_color(rgb)

    @objc.python_method
    def apply_color(self, rgb):
        brightness = self.config.get("brightness", 100)
        self.attempt(lambda: control.set_backlight(
            config.apply_brightness(rgb, brightness)))
        self.mutate(lambda s: s.update(backlight=config.format_color(rgb)))
        self.rebuild()

    @objc.IBAction
    def pickScreen_(self, sender):
        name = sender.representedObject()
        self.mutate(lambda s: s.setdefault("lcd", {}).update(
            screen=name, enabled=True))
        self.attempt(lambda: control.show_screen(name))
        self.rebuild()

    @objc.IBAction
    def disableLcd_(self, _sender):
        self.mutate(lambda s: s.setdefault("lcd", {}).update(enabled=False))
        self.attempt(control.clear_lcd)
        self.rebuild()

    @objc.IBAction
    def toggleAgent_(self, _sender):
        if cli.agent_loaded():
            subprocess.run(["launchctl", "unload", "-w", cli.PLIST_PATH],
                           capture_output=True)
        else:
            try:
                cli.cmd_start([])
            except SystemExit as exc:
                log_failure(str(exc))
        self.rebuild()

    @objc.IBAction
    def editConfig_(self, _sender):
        config.ensure_exists()
        NSWorkspace.sharedWorkspace().openFile_(config.CONFIG_PATH)

    @objc.IBAction
    def refresh_(self, _sender):
        self.rebuild()

    @objc.IBAction
    def showWindow_(self, _sender):
        if getattr(self, "window_controller", None) is not None:
            self.window_controller.show()

    @objc.IBAction
    def quit_(self, _sender):
        NSApplication.sharedApplication().terminate_(None)


class AppDelegate(NSObject):
    """Bring the window back when the Dock icon is clicked."""

    def initWithController_(self, controller):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.controller = controller
        return self

    def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, visible):
        if not visible and self.controller is not None:
            self.controller.show()
        return True


def name_the_app():
    """Make the Dock and menu say G510 rather than Python.

    The bundle execs an interpreter that lives outside it, so macOS identifies
    the process as Python. Patching the main bundle's info dictionary before
    NSApplication starts is the standard fix for a PyObjC app.
    """
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = "G510"
            info["CFBundleDisplayName"] = "G510"
    except Exception:
        pass


def log_failure(message):
    """Surface a failure that cannot be raised out of a menu action."""
    alert = NSAlert.alloc().init()
    alert.setMessageText_("That did not work")
    alert.setInformativeText_(message or "No detail available.")
    alert.addButtonWithTitle_("OK")
    alert.runModal()


def main():
    import window

    name_the_app()
    app = NSApplication.sharedApplication()
    # Regular, not Accessory: a crowded or notched menu bar silently drops
    # overflow status items, so the app needs a Dock icon and a real window.
    menu_only = "--menu-only" in sys.argv
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory if menu_only
                             else NSApplicationActivationPolicyRegular)
    menu = G510Menu.alloc().init()
    controller = None
    if not menu_only:
        controller = window.G510Window.alloc().initWithMenu_(menu)
        menu.window_controller = controller
        delegate = AppDelegate.alloc().initWithController_(controller)
        app.setDelegate_(delegate)
        controller.show()
    app.run()


if __name__ == "__main__":
    main()
