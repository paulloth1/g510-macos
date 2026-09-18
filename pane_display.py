"""The Display pane: which LCD screen is showing, the soft keys, the printer.

Layout, refresh and the bodies of this pane's actions, as plain functions
taking the window controller. The controller keeps the selectors - AppKit
needs one object to send them to - and calls in here.
"""
from AppKit import (NSAlert, NSBezelStyleRounded, NSButton, NSColor, NSFont,
                    NSImageView, NSMakeRect, NSPopUpButton, NSSwitchButton,
                    NSTextField)

import control
import printer
import screens
from lcd import Canvas
from widgets import CARD_WIDTH, SCREEN_LABELS, place, text


# -- layout ----------------------------------------------------------------

def fill_screen(win, content, height):
    win.screen_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
        NSMakeRect(16, height - 44, 180, 26), False)
    for name in screens.SCREENS:
        win.screen_popup.addItemWithTitle_(SCREEN_LABELS.get(name, name.title()))
    win.screen_popup.addItemWithTitle_("Off")
    win.screen_popup.setTarget_(win)
    win.screen_popup.setAction_(b"screenChanged:")
    content.addSubview_(win.screen_popup)
    win.showing_label = text("", 11, secondary=True)
    content.addSubview_(place(win.showing_label, 206, height - 38, 260, 15))

    content.addSubview_(place(
        text("Buttons under the screen, left to right", 11, secondary=True),
        16, 50, 300, 15))
    win.lcd_key_popups = {}
    x = 14
    for key in ("L2", "L3", "L4", "L5"):
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(x, 14, 110, 24), False)
        for name in screens.SCREENS:
            popup.addItemWithTitle_(SCREEN_LABELS.get(name, name.title()))
        for extra in ("Next", "Previous", "Off"):
            popup.addItemWithTitle_(extra)
        popup.setFont_(NSFont.systemFontOfSize_(11))
        popup.setTarget_(win)
        popup.setAction_(b"lcdKeyChanged:")
        popup.setIdentifier_(key)
        content.addSubview_(popup)
        win.lcd_key_popups[key] = popup
        x += 114

    win.preview = NSImageView.alloc().initWithFrame_(
        NSMakeRect((CARD_WIDTH - 320) / 2, 76, 320, 86))
    win.preview.setWantsLayer_(True)
    layer = win.preview.layer()
    layer.setCornerRadius_(5.0)
    layer.setMasksToBounds_(True)
    layer.setBorderWidth_(1.0)
    layer.setBorderColor_(NSColor.separatorColor().CGColor())
    content.addSubview_(win.preview)


def fill_claude(win, content, height):
    content.addSubview_(place(
        text("Which usage windows the Claude screen shows", 11,
             secondary=True), 16, height - 26, 340, 15))
    win.claude_popups = []
    for index in range(2):
        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(16 + index * 176, 12, 168, 24), False)
        for key in screens.CLAUDE_ROWS:
            popup.addItemWithTitle_(key.replace("_", " ").title())
        popup.addItemWithTitle_("None")
        popup.setFont_(NSFont.systemFontOfSize_(11))
        popup.setTarget_(win)
        popup.setAction_(b"claudeRowChanged:")
        content.addSubview_(popup)
        win.claude_popups.append(popup)


def fill_printer(win, content, height):
    win.printer_enabled = NSButton.alloc().initWithFrame_(
        NSMakeRect(16, height - 32, 200, 20))
    win.printer_enabled.setButtonType_(NSSwitchButton)
    win.printer_enabled.setTitle_("Show the printer screen")
    win.printer_enabled.setTarget_(win)
    win.printer_enabled.setAction_(b"printerToggled:")
    content.addSubview_(win.printer_enabled)

    content.addSubview_(place(text("Address", 11, secondary=True),
                              16, height - 58, 60, 15))
    win.printer_host = NSTextField.alloc().initWithFrame_(
        NSMakeRect(80, height - 62, 220, 22))
    win.printer_host.setPlaceholderString_("192.168.1.50")
    win.printer_host.setTarget_(win)
    win.printer_host.setAction_(b"printerHostChanged:")
    content.addSubview_(win.printer_host)

    test = NSButton.alloc().initWithFrame_(NSMakeRect(310, height - 63, 90, 24))
    test.setTitle_("Test")
    test.setBezelStyle_(NSBezelStyleRounded)
    test.setFont_(NSFont.systemFontOfSize_(11))
    test.setTarget_(win)
    test.setAction_(b"printerTest:")
    content.addSubview_(test)

    win.printer_status = text("", 11, secondary=True)
    content.addSubview_(place(win.printer_status, 16, 12, 460, 15))


# -- state -----------------------------------------------------------------

def refresh_preview(win):
    lcd = win.config.get("lcd", {})
    canvas = Canvas()
    if lcd.get("enabled", True):
        name = lcd.get("screen", "status")
        screens.SCREENS.get(name, screens.screen_status)(canvas, {})
    win.preview.setImage_(canvas.preview(2))


def refresh(win):
    lcd = win.config.get("lcd", {})
    enabled = lcd.get("enabled", True)
    active = lcd.get("screen", "status")
    win.screen_popup.selectItemWithTitle_(
        SCREEN_LABELS.get(active, active.title()) if enabled else "Off")
    win.showing_label.setStringValue_(
        "showing now" if enabled else "display is blank")
    lcd_keys = win.config.get("lcd_keys") or {}
    for key, popup in win.lcd_key_popups.items():
        value = lcd_keys.get(key) or "next"
        if isinstance(value, str) and value.startswith("screen:"):
            name = value.split(":", 1)[1]
            popup.selectItemWithTitle_(SCREEN_LABELS.get(name, name.title()))
        else:
            popup.selectItemWithTitle_(str(value).title())
    refresh_preview(win)

    rows = (win.config.get("claude") or {}).get("rows") or []
    for index, popup in enumerate(win.claude_popups):
        wanted = rows[index] if index < len(rows) else None
        popup.selectItemWithTitle_(
            wanted.replace("_", " ").title() if wanted else "None")

    block = win.config.get("printer") or {}
    win.printer_enabled.setState_(1 if block.get("enabled") else 0)
    if win.window.firstResponder() is not win.printer_host.currentEditor():
        win.printer_host.setStringValue_(block.get("host") or "")
    reading = printer.status()
    if not block.get("enabled") or not block.get("host"):
        win.printer_status.setStringValue_("Not configured")
    elif reading:
        detail = f"{reading['model']} · {printer.describe_state(reading)}"
        if printer.is_printing(reading):
            detail += f" · {reading['progress']}%"
        win.printer_status.setStringValue_(detail)
    else:
        win.printer_status.setStringValue_(
            printer.last_error() or "waiting for a reply…")


# -- actions ---------------------------------------------------------------

def screen_changed(win, sender):
    index = int(sender.indexOfSelectedItem())
    names = list(screens.SCREENS)
    choice = names[index] if index < len(names) else "off"

    def apply(settings):
        lcd = settings.setdefault("lcd", {})
        lcd["enabled"] = choice != "off"
        if choice != "off":
            lcd["screen"] = choice

    win.mutate(apply)
    try:
        control.clear_lcd() if choice == "off" else control.show_screen(choice)
    except control.ControlError:
        pass
    win.refresh()


def lcd_key_changed(win, sender):
    key = str(sender.identifier())
    index = int(sender.indexOfSelectedItem())
    names = list(screens.SCREENS)
    if index < len(names):
        value = f"screen:{names[index]}"
    else:
        value = ["next", "prev", "off"][index - len(names)]
    win.mutate(lambda s: s.setdefault("lcd_keys", {}).update({key: value}))


def claude_row_changed(win, _sender):
    keys = list(screens.CLAUDE_ROWS)
    chosen = []
    for popup in win.claude_popups:
        index = int(popup.indexOfSelectedItem())
        if index < len(keys) and keys[index] not in chosen:
            chosen.append(keys[index])
    win.mutate(lambda s: s.setdefault("claude", {}).update(rows=chosen))
    win.refresh()


def printer_toggled(win, sender):
    wanted = bool(sender.state())
    win.mutate(lambda s: s.setdefault("printer", {}).update(enabled=wanted))
    win.refresh()


def printer_host_changed(win, sender):
    host = str(sender.stringValue()).strip()
    win.mutate(lambda s: s.setdefault("printer", {}).update(host=host))
    win.refresh()


def printer_test(win):
    printer_host_changed(win, win.printer_host)
    reading = printer.status(force=True)
    alert = NSAlert.alloc().init()
    if reading:
        alert.setMessageText_(f"{reading['model']} answered")
        alert.setInformativeText_(
            f"{printer.describe_state(reading)} · {reading['progress']}%\n"
            f"{reading['file'] or 'no job'}")
    else:
        alert.setMessageText_("No reply")
        alert.setInformativeText_(printer.last_error() or "Unknown error.")
    alert.addButtonWithTitle_("OK")
    alert.runModal()
    win.refresh()
