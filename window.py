"""The G510 window, laid out the way a Mac settings window is.

Four panes behind a segmented control, each a column of grouped cards. Panes
size themselves from the cards they hold, so adding a control cannot silently
push another one off the bottom.
"""
import os
import subprocess
import time

import objc
from AppKit import (NSAlert, NSApplication, NSBackingStoreBuffered,
                    NSBezelStyleRounded, NSBox, NSButton, NSColor, NSColorWell,
                    NSFont, NSImage, NSImageView, NSMakePoint, NSMakeRect,
                    NSMakeSize, NSPopUpButton, NSScreen, NSScrollView,
                    NSSegmentedControl, NSSlider, NSSwitchButton, NSTableColumn,
                    NSTableView, NSTextField, NSView, NSWindow,
                    NSWindowStyleMaskClosable,
                    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled, NSWorkspace)
from Foundation import NSObject, NSTimer

import actions
import cli
import config
import control
import device
import ipc
import printer
import recorder
import screens
from lcd import Canvas

WIDTH = 520
MARGIN = 20
GAP = 24
CARD_WIDTH = WIDTH - MARGIN * 2
MIN_HEIGHT = 380
MAX_HEIGHT = 900

PANES = ("Keyboard", "Display", "Keys", "Agent")

SWATCHES = ["white", "red", "orange", "yellow", "green",
            "cyan", "blue", "purple", "pink", "off"]

BINDING_TYPES = ["app", "keys", "shell", "text", "macro", "none"]
TYPE_HINTS = {
    "app": "Application name, e.g. Safari",
    "keys": "Key chord, e.g. cmd+shift+4",
    "shell": "Shell command, e.g. open -a Terminal",
    "text": "Text to type",
    "macro": "Name of a recorded macro",
    "none": "Nothing - clears the binding",
}

SCREEN_LABELS = {
    "status": "Status", "clock": "Clock", "claude": "Claude Usage",
    "media": "Now Playing", "printer": "3D Printer", "gkeys": "G-key Echo",
    "app": "Active App",
}

SWITCH_ACTIONS = ["nothing", "bank:1", "bank:2", "bank:3"] + \
    [f"screen:{name}" for name in SCREEN_LABELS]

NSBoxCustom = 4
NSNoTitle = 0
NSTableViewStyleInset = 2
NSLineBreakByTruncatingTail = 4
NSViewWidthSizable, NSViewHeightSizable = 2, 16


def text(value, size=12, bold=False, secondary=False):
    field = NSTextField.alloc().init()
    field.setStringValue_(value)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                   else NSFont.systemFontOfSize_(size))
    if secondary:
        field.setTextColor_(NSColor.secondaryLabelColor())
    field.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
    return field


def place(view, x, y, width, height):
    view.setFrame_(NSMakeRect(x, y, width, height))
    return view


def swatch_image(rgb, size=18):
    """A filled circle, for the colour preset buttons."""
    image = NSImage.alloc().initWithSize_(NSMakeSize(size, size))
    image.lockFocus()
    red, green, blue = (channel / 255.0 for channel in rgb)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, 1.0).setFill()
    path = objc.lookUpClass("NSBezierPath").bezierPathWithOvalInRect_(
        NSMakeRect(1, 1, size - 2, size - 2))
    path.fill()
    NSColor.separatorColor().setStroke()
    path.setLineWidth_(1.0)
    path.stroke()
    image.unlockFocus()
    return image


def symbol(name, size=15):
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is not None:
        image.setTemplate_(True)
        image.setSize_(NSMakeSize(size, size))
    return image


def _gkey_order(name):
    """Sort G1..G18 numerically, tolerating junk from a hand-edited config."""
    digits = name[1:] if name[:1].upper() == "G" else ""
    return (0, int(digits)) if digits.isdigit() else (1, name)


def switch_label(value):
    """'screen:claude' -> 'Claude Usage', for the switch popups."""
    if not value or value == "nothing":
        return "Nothing"
    if value.startswith("bank:"):
        return f"Bank M{value.split(':', 1)[1]}"
    if value.startswith("screen:"):
        return SCREEN_LABELS.get(value.split(':', 1)[1], value)
    return str(value)


class G510Window(NSObject):

    def initWithMenu_(self, menu):
        self = objc.super(G510Window, self).init()
        if self is None:
            return None
        self.menu = menu
        self.config = config.load()
        self.editing_key = None
        self.editing_app = None
        self.panes = {}
        self.build()
        self.start_timer()
        return self

    # -- construction ------------------------------------------------------

    @objc.python_method
    def card(self, parent, parent_height, y_from_top, height, title=None):
        """A rounded settings-style group, returned with its content view."""
        y = parent_height - y_from_top - height
        box = NSBox.alloc().initWithFrame_(
            NSMakeRect(MARGIN, y, CARD_WIDTH, height))
        box.setBoxType_(NSBoxCustom)
        box.setTitlePosition_(NSNoTitle)
        box.setCornerRadius_(10.0)
        box.setBorderWidth_(1.0)
        box.setBorderColor_(NSColor.separatorColor())
        box.setFillColor_(NSColor.controlBackgroundColor())
        box.setContentViewMargins_(NSMakeSize(0, 0))
        parent.addSubview_(box)
        if title:
            header = text(title, 11, bold=True, secondary=True)
            place(header, MARGIN, parent_height - y_from_top + 4, 300, 15)
            parent.addSubview_(header)
        return box.contentView()

    @objc.python_method
    def build_pane(self, sections):
        """Lay out a column of cards, sizing the pane to what it holds."""
        total = MARGIN + sum(h + GAP for h, _, _ in sections) - GAP + MARGIN
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, total))
        cursor = MARGIN
        for height, title, fill in sections:
            fill(self.card(view, total, cursor, height, title), height)
            cursor += height + GAP
        return view

    @objc.python_method
    def build(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, 640), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("G510")
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSMakeSize(WIDTH, MIN_HEIGHT))
        self.window.setContentMaxSize_(NSMakeSize(WIDTH, MAX_HEIGHT))
        root = self.window.contentView()

        self.picker = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(MARGIN, 0, CARD_WIDTH, 24))
        self.picker.setSegmentCount_(len(PANES))
        for index, name in enumerate(PANES):
            self.picker.setLabel_forSegment_(name, index)
            self.picker.setWidth_forSegment_(CARD_WIDTH / len(PANES), index)
        self.picker.setSelectedSegment_(0)
        self.picker.setTarget_(self)
        self.picker.setAction_(b"paneChanged:")
        root.addSubview_(self.picker)

        self.scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 100))
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setDrawsBackground_(False)
        self.scroll.setBorderType_(0)
        self.scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        root.addSubview_(self.scroll)

        self.panes["Keyboard"] = self.build_pane([
            (62, None, self.fill_device),
            (118, "BACKLIGHT", self.fill_backlight),
            (86, "JOYSTICK SWITCH", self.fill_switch),
        ])
        self.panes["Display"] = self.build_pane([
            (222, "SCREEN", self.fill_screen),
            (62, "CLAUDE USAGE", self.fill_claude),
            (104, "3D PRINTER", self.fill_printer),
        ])
        self.panes["Keys"] = self.build_pane([
            (254, "G-KEY BINDINGS", self.fill_bindings),
            (170, "PER-APP OVERRIDES", self.fill_profiles),
        ])
        self.panes["Agent"] = self.build_pane([
            (92, "BACKGROUND AGENT", self.fill_agent),
            (76, "CONFIGURATION FILE", self.fill_configfile),
        ])

        self.show_pane("Keyboard")
        self.refresh()

    @objc.python_method
    def show_pane(self, name):
        """Swap the pane in and size the window to it."""
        pane = self.panes[name]
        height = pane.frame().size.height
        screen = NSScreen.mainScreen() or NSScreen.screens()[0]
        usable = int(screen.visibleFrame().size.height) - 120
        visible = max(MIN_HEIGHT, min(height, MAX_HEIGHT, usable))

        frame = self.window.frame()
        self.window.setContentSize_(NSMakeSize(WIDTH, visible + 40))
        self.window.setFrameTopLeftPoint_(NSMakePoint(frame.origin.x,
                                                      frame.origin.y + frame.size.height))
        root = self.window.contentView()
        top = root.frame().size.height
        place(self.picker, MARGIN, top - 32, CARD_WIDTH, 24)
        place(self.scroll, 0, 0, WIDTH, top - 40)
        self.scroll.setDocumentView_(pane)
        clip = self.scroll.contentView()
        clip.scrollToPoint_(NSMakePoint(0, height - clip.bounds().size.height))
        self.scroll.reflectScrolledClipView_(clip)

    # -- Keyboard pane -----------------------------------------------------

    @objc.python_method
    def fill_device(self, content, height):
        icon = NSImageView.alloc().init()
        icon.setImage_(symbol("keyboard", 26))
        icon.setContentTintColor_(NSColor.secondaryLabelColor())
        content.addSubview_(place(icon, 16, height - 42, 30, 26))
        self.device_title = text("G510 Gaming Keyboard", 13, bold=True)
        content.addSubview_(place(self.device_title, 56, height - 36, 380, 18))
        self.device_detail = text("", 11, secondary=True)
        content.addSubview_(place(self.device_detail, 56, height - 54, 380, 15))

    @objc.python_method
    def fill_backlight(self, content, height):
        self.color_well = NSColorWell.alloc().initWithFrame_(
            NSMakeRect(16, height - 44, 56, 28))
        self.color_well.setTarget_(self)
        self.color_well.setAction_(b"colorChanged:")
        content.addSubview_(self.color_well)
        self.color_label = text("", 12)
        content.addSubview_(place(self.color_label, 84, height - 38, 120, 17))

        self.brightness_slider = NSSlider.alloc().initWithFrame_(
            NSMakeRect(84, height - 62, 250, 20))
        self.brightness_slider.setMinValue_(0)
        self.brightness_slider.setMaxValue_(100)
        self.brightness_slider.setTarget_(self)
        self.brightness_slider.setAction_(b"brightnessChanged:")
        self.brightness_slider.setContinuous_(True)
        content.addSubview_(self.brightness_slider)
        self.brightness_label = text("", 11, secondary=True)
        content.addSubview_(place(self.brightness_label, 342, height - 60, 90, 15))

        x = 16
        for name in SWATCHES:
            button = NSButton.alloc().initWithFrame_(NSMakeRect(x, 14, 28, 28))
            button.setImage_(swatch_image(config.NAMED_COLORS[name]))
            button.setBordered_(False)
            button.setToolTip_(name.title())
            button.setTarget_(self)
            button.setAction_(b"presetClicked:")
            button.setIdentifier_(name)
            content.addSubview_(button)
            x += 34

    @objc.python_method
    def fill_switch(self, content, height):
        content.addSubview_(place(
            text("The switch above the F-keys, which locks the ⌘ key in "
                 "hardware.", 11, secondary=True), 16, height - 26, 460, 15))
        self.switch_popups = {}
        for index, (key, label) in enumerate((("on", "Engaged"),
                                              ("off", "Released"))):
            x = 16 + index * 236
            content.addSubview_(place(text(label, 11, secondary=True),
                                      x, height - 44, 100, 15))
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(x, 14, 210, 26), False)
            for value in SWITCH_ACTIONS:
                popup.addItemWithTitle_(switch_label(value))
            popup.setTarget_(self)
            popup.setAction_(b"switchChanged:")
            popup.setIdentifier_(key)
            content.addSubview_(popup)
            self.switch_popups[key] = popup

    # -- Display pane ------------------------------------------------------

    @objc.python_method
    def fill_screen(self, content, height):
        self.screen_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(16, height - 44, 180, 26), False)
        for name in screens.SCREENS:
            self.screen_popup.addItemWithTitle_(SCREEN_LABELS.get(name, name.title()))
        self.screen_popup.addItemWithTitle_("Off")
        self.screen_popup.setTarget_(self)
        self.screen_popup.setAction_(b"screenChanged:")
        content.addSubview_(self.screen_popup)
        self.showing_label = text("", 11, secondary=True)
        content.addSubview_(place(self.showing_label, 206, height - 38, 260, 15))

        content.addSubview_(place(
            text("Buttons under the screen, left to right", 11, secondary=True),
            16, 50, 300, 15))
        self.lcd_key_popups = {}
        x = 14
        for key in ("L2", "L3", "L4", "L5"):
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(x, 14, 110, 24), False)
            for name in screens.SCREENS:
                popup.addItemWithTitle_(SCREEN_LABELS.get(name, name.title()))
            for extra in ("Next", "Previous", "Off"):
                popup.addItemWithTitle_(extra)
            popup.setFont_(NSFont.systemFontOfSize_(11))
            popup.setTarget_(self)
            popup.setAction_(b"lcdKeyChanged:")
            popup.setIdentifier_(key)
            content.addSubview_(popup)
            self.lcd_key_popups[key] = popup
            x += 114

        self.preview = NSImageView.alloc().initWithFrame_(
            NSMakeRect((CARD_WIDTH - 320) / 2, 76, 320, 86))
        self.preview.setWantsLayer_(True)
        layer = self.preview.layer()
        layer.setCornerRadius_(5.0)
        layer.setMasksToBounds_(True)
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(NSColor.separatorColor().CGColor())
        content.addSubview_(self.preview)

    @objc.python_method
    def fill_claude(self, content, height):
        content.addSubview_(place(
            text("Which usage windows the Claude screen shows", 11,
                 secondary=True), 16, height - 26, 340, 15))
        self.claude_popups = []
        for index in range(2):
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(16 + index * 176, 12, 168, 24), False)
            for key in screens.CLAUDE_ROWS:
                popup.addItemWithTitle_(key.replace("_", " ").title())
            popup.addItemWithTitle_("None")
            popup.setFont_(NSFont.systemFontOfSize_(11))
            popup.setTarget_(self)
            popup.setAction_(b"claudeRowChanged:")
            content.addSubview_(popup)
            self.claude_popups.append(popup)

    @objc.python_method
    def fill_printer(self, content, height):
        self.printer_enabled = NSButton.alloc().initWithFrame_(
            NSMakeRect(16, height - 32, 200, 20))
        self.printer_enabled.setButtonType_(NSSwitchButton)
        self.printer_enabled.setTitle_("Show the printer screen")
        self.printer_enabled.setTarget_(self)
        self.printer_enabled.setAction_(b"printerToggled:")
        content.addSubview_(self.printer_enabled)

        content.addSubview_(place(text("Address", 11, secondary=True),
                                  16, height - 58, 60, 15))
        self.printer_host = NSTextField.alloc().initWithFrame_(
            NSMakeRect(80, height - 62, 220, 22))
        self.printer_host.setPlaceholderString_("192.168.1.50")
        self.printer_host.setTarget_(self)
        self.printer_host.setAction_(b"printerHostChanged:")
        content.addSubview_(self.printer_host)

        test = NSButton.alloc().initWithFrame_(NSMakeRect(310, height - 63, 90, 24))
        test.setTitle_("Test")
        test.setBezelStyle_(NSBezelStyleRounded)
        test.setFont_(NSFont.systemFontOfSize_(11))
        test.setTarget_(self)
        test.setAction_(b"printerTest:")
        content.addSubview_(test)

        self.printer_status = text("", 11, secondary=True)
        content.addSubview_(place(self.printer_status, 16, 12, 460, 15))

    # -- Keys pane ---------------------------------------------------------

    @objc.python_method
    def fill_bindings(self, content, height):
        self.bank_picker = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(CARD_WIDTH - 152, height - 26, 138, 22))
        self.bank_picker.setSegmentCount_(3)
        for index, name in enumerate(config.BANKS):
            self.bank_picker.setLabel_forSegment_(f"M{name}", index)
            self.bank_picker.setWidth_forSegment_(46, index)
        self.bank_picker.setTarget_(self)
        self.bank_picker.setAction_(b"bankChanged:")
        content.addSubview_(self.bank_picker)

        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(1, 30, CARD_WIDTH - 2, height - 61))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        scroll.setDrawsBackground_(False)
        self.table = self._make_table(("Key", 56), ("Action", 392))
        self.table.setDoubleAction_(b"editBinding:")
        scroll.setDocumentView_(self.table)
        content.addSubview_(scroll)

        content.addSubview_(place(
            text("Double-click a row to change it", 11, secondary=True),
            14, 8, 200, 15))
        record = NSButton.alloc().initWithFrame_(
            NSMakeRect(CARD_WIDTH - 268, 5, 116, 22))
        record.setTitle_("Record macro…")
        record.setBezelStyle_(NSBezelStyleRounded)
        record.setFont_(NSFont.systemFontOfSize_(11))
        record.setTarget_(self)
        record.setAction_(b"recordMacro:")
        content.addSubview_(record)
        edit = NSButton.alloc().initWithFrame_(
            NSMakeRect(CARD_WIDTH - 148, 5, 134, 22))
        edit.setTitle_("Edit JSON…")
        edit.setBezelStyle_(NSBezelStyleRounded)
        edit.setFont_(NSFont.systemFontOfSize_(11))
        edit.setTarget_(self)
        edit.setAction_(b"editConfig:")
        content.addSubview_(edit)

    @objc.python_method
    def fill_profiles(self, content, height):
        content.addSubview_(place(
            text("Bindings that apply only while an app is frontmost", 11,
                 secondary=True), 16, height - 26, 400, 15))
        self.profile_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(14, height - 56, 240, 24), False)
        self.profile_popup.setTarget_(self)
        self.profile_popup.setAction_(b"profileAppChanged:")
        self.profile_popup.setFont_(NSFont.systemFontOfSize_(11))
        content.addSubview_(self.profile_popup)

        add = NSButton.alloc().initWithFrame_(
            NSMakeRect(CARD_WIDTH - 148, height - 56, 134, 24))
        add.setTitle_("Add override…")
        add.setBezelStyle_(NSBezelStyleRounded)
        add.setFont_(NSFont.systemFontOfSize_(11))
        add.setTarget_(self)
        add.setAction_(b"addProfileBinding:")
        content.addSubview_(add)

        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(1, 8, CARD_WIDTH - 2, height - 72))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        scroll.setDrawsBackground_(False)
        self.profile_table = self._make_table(("Key", 56), ("Override", 392))
        self.profile_table.setDoubleAction_(b"editProfileBinding:")
        scroll.setDocumentView_(self.profile_table)
        content.addSubview_(scroll)

    @objc.python_method
    def _make_table(self, *columns):
        table = NSTableView.alloc().init()
        for title, width in columns:
            column = NSTableColumn.alloc().initWithIdentifier_(title.lower())
            column.headerCell().setStringValue_(title)
            column.setWidth_(width)
            table.addTableColumn_(column)
        try:
            table.setStyle_(NSTableViewStyleInset)
        except AttributeError:
            pass
        table.setRowHeight_(22.0)
        table.setUsesAlternatingRowBackgroundColors_(False)
        table.setGridStyleMask_(0)
        table.setBackgroundColor_(NSColor.clearColor())
        table.setDataSource_(self)
        table.setDelegate_(self)
        table.setTarget_(self)
        return table

    # -- Agent pane --------------------------------------------------------

    @objc.python_method
    def fill_agent(self, content, height):
        self.agent_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(16, height - 46, 130, 30))
        self.agent_button.setBezelStyle_(NSBezelStyleRounded)
        self.agent_button.setTarget_(self)
        self.agent_button.setAction_(b"toggleAgent:")
        content.addSubview_(self.agent_button)
        self.agent_label = text("", 11, secondary=True)
        content.addSubview_(place(self.agent_label, 156, height - 40, 330, 15))
        self.permission_label = text("", 11, secondary=True)
        content.addSubview_(place(self.permission_label, 16, 16, 340, 15))
        self.permission_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(CARD_WIDTH - 148, 12, 134, 22))
        self.permission_button.setTitle_("Permissions…")
        self.permission_button.setBezelStyle_(NSBezelStyleRounded)
        self.permission_button.setFont_(NSFont.systemFontOfSize_(11))
        self.permission_button.setTarget_(self)
        self.permission_button.setAction_(b"askPermissions:")
        content.addSubview_(self.permission_button)

    @objc.python_method
    def fill_configfile(self, content, height):
        content.addSubview_(place(text(config.CONFIG_PATH, 11, secondary=True),
                                  16, height - 30, 460, 15))
        open_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(16, 14, 160, 26))
        open_button.setTitle_("Open in editor")
        open_button.setBezelStyle_(NSBezelStyleRounded)
        open_button.setTarget_(self)
        open_button.setAction_(b"editConfig:")
        content.addSubview_(open_button)
        self.cli_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(356, 14, 130, 26))
        self.cli_button.setTitle_("Install g510")
        self.cli_button.setBezelStyle_(NSBezelStyleRounded)
        self.cli_button.setToolTip_("Put the g510 command on your PATH")
        self.cli_button.setTarget_(self)
        self.cli_button.setAction_(b"installCli:")
        content.addSubview_(self.cli_button)
        self.log_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(186, 14, 160, 26))
        self.log_button.setTitle_("Open agent log")
        self.log_button.setBezelStyle_(NSBezelStyleRounded)
        self.log_button.setTarget_(self)
        self.log_button.setAction_(b"openLog:")
        content.addSubview_(self.log_button)

    # -- state -------------------------------------------------------------

    @objc.python_method
    def start_timer(self):
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, b"tick:", None, True)

    def tick_(self, _timer):
        """Keep the window honest: the keyboard is driven from elsewhere too."""
        if not self.window.isVisible():
            return
        try:
            self.refresh()
        except Exception:
            try:
                self.refresh_preview()
            except Exception:
                pass

    @objc.python_method
    def refresh_preview(self):
        lcd = self.config.get("lcd", {})
        canvas = Canvas()
        if lcd.get("enabled", True):
            name = lcd.get("screen", "status")
            screens.SCREENS.get(name, screens.screen_status)(canvas, {})
        self.preview.setImage_(canvas.preview(2))

    @objc.python_method
    def refresh(self):
        self.config = config.load()
        running = control.daemon_running()
        connected = running or device.find_path() is not None
        self.device_title.setStringValue_(
            "G510 Gaming Keyboard" if connected else "No G510 found")
        if connected:
            detail = "Connected"
            if running:
                detail += " · agent holds the device"
            self.device_detail.setTextColor_(NSColor.secondaryLabelColor())
        else:
            detail = "Plug the keyboard in, or check the dock"
            self.device_detail.setTextColor_(NSColor.systemRedColor())
        self.device_detail.setStringValue_(detail)

        brightness = int(self.config.get("brightness", 100))
        self.brightness_slider.setIntValue_(brightness)
        self.brightness_label.setStringValue_(f"{brightness}% brightness")
        try:
            rgb = config.parse_color(self.config.get("backlight", "#ffffff"))
            self.color_well.setColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, 1.0))
            self.color_label.setStringValue_(config.format_color(rgb))
        except config.ColorError:
            self.color_label.setStringValue_("-")

        switch = self.config.get("game_switch") or {}
        for key, popup in self.switch_popups.items():
            popup.selectItemWithTitle_(switch_label(switch.get(key)))

        lcd = self.config.get("lcd", {})
        enabled = lcd.get("enabled", True)
        active = lcd.get("screen", "status")
        self.screen_popup.selectItemWithTitle_(
            SCREEN_LABELS.get(active, active.title()) if enabled else "Off")
        self.showing_label.setStringValue_(
            "showing now" if enabled else "display is blank")
        lcd_keys = self.config.get("lcd_keys") or {}
        for key, popup in self.lcd_key_popups.items():
            value = lcd_keys.get(key) or "next"
            if isinstance(value, str) and value.startswith("screen:"):
                name = value.split(":", 1)[1]
                popup.selectItemWithTitle_(SCREEN_LABELS.get(name, name.title()))
            else:
                popup.selectItemWithTitle_(str(value).title())
        self.refresh_preview()

        rows = (self.config.get("claude") or {}).get("rows") or []
        for index, popup in enumerate(self.claude_popups):
            wanted = rows[index] if index < len(rows) else None
            popup.selectItemWithTitle_(
                wanted.replace("_", " ").title() if wanted else "None")

        block = self.config.get("printer") or {}
        self.printer_enabled.setState_(1 if block.get("enabled") else 0)
        if self.window.firstResponder() is not self.printer_host.currentEditor():
            self.printer_host.setStringValue_(block.get("host") or "")
        reading = printer.status()
        if not block.get("enabled") or not block.get("host"):
            self.printer_status.setStringValue_("Not configured")
        elif reading:
            detail = f"{reading['model']} · {printer.describe_state(reading)}"
            if printer.is_printing(reading):
                detail += f" · {reading['progress']}%"
            self.printer_status.setStringValue_(detail)
        else:
            self.printer_status.setStringValue_(
                printer.last_error() or "waiting for a reply…")

        bank = str(self.config.get("active_bank", "1"))
        if bank in config.BANKS:
            self.bank_picker.setSelectedSegment_(config.BANKS.index(bank))
        self.refresh_profiles()
        self.table.reloadData()
        self.profile_table.reloadData()

        loaded = self.agent_loaded_cached()
        self.agent_button.setTitle_("Stop" if loaded else "Start")
        self.agent_label.setStringValue_(
            "Running, and starts at login" if loaded
            else "Not running - G-keys will not do anything")
        posting = actions.can_post_events()
        reading_ok = actions.can_read_input()
        if posting and reading_ok:
            self.permission_label.setTextColor_(NSColor.secondaryLabelColor())
            self.permission_label.setStringValue_(
                "Input Monitoring and Accessibility granted")
            self.permission_button.setHidden_(True)
        else:
            missing = ([] if reading_ok else ["Input Monitoring"]) + \
                      ([] if posting else ["Accessibility"])
            self.permission_label.setTextColor_(NSColor.systemOrangeColor())
            self.permission_label.setStringValue_(f"Needed: {', '.join(missing)}")
            self.permission_button.setHidden_(False)

    @objc.python_method
    def refresh_profiles(self):
        """Keep the app popup listing every app with overrides, plus this one."""
        profiles = self.config.get("app_profiles") or {}
        front = screens.frontmost_app()
        names = sorted(profiles)
        if front and front not in names:
            names.insert(0, front)
        chosen = self.editing_app if self.editing_app in names else (
            names[0] if names else None)
        self.editing_app = chosen
        titles = [str(self.profile_popup.itemTitleAtIndex_(i))
                  for i in range(self.profile_popup.numberOfItems())]
        if titles != names:
            self.profile_popup.removeAllItems()
            for name in names:
                self.profile_popup.addItemWithTitle_(name)
        if chosen:
            self.profile_popup.selectItemWithTitle_(chosen)

    @objc.python_method
    def profile_rows(self):
        profiles = (self.config.get("app_profiles") or {}).get(self.editing_app) or {}
        return sorted(profiles.items(), key=lambda pair: _gkey_order(pair[0]))

    @objc.python_method
    def agent_loaded_cached(self, max_age=5.0):
        """launchctl costs ~8ms and the answer rarely changes; cache it."""
        now = time.time()
        cached = getattr(self, "_agent_cache", None)
        if cached and now - cached[0] < max_age:
            return cached[1]
        value = cli.agent_loaded()
        self._agent_cache = (now, value)
        return value

    @objc.python_method
    def invalidate_agent_cache(self):
        self._agent_cache = None

    @objc.python_method
    def mutate(self, apply):
        """Re-read, change, write back.

        The agent and the CLI write this file too, and the window holds its
        copy for up to a second between refreshes. Saving that copy wholesale
        would revert anything they changed in the meantime.
        """
        fresh = config.load()
        apply(fresh)
        if not config.save(fresh):
            self.complain(config.last_error or "the config could not be saved")
            return fresh
        self.config = fresh
        cli.reload_agent()
        if self.menu is not None:
            self.menu.rebuild()
        return fresh

    @objc.python_method
    def show(self):
        self.center_on_active_screen()
        self.window.makeKeyAndOrderFront_(None)
        self.window.orderFrontRegardless()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.refresh()

    @objc.python_method
    def center_on_active_screen(self):
        """Put the window on the screen being used, not a secondary display."""
        screen = NSScreen.mainScreen() or NSScreen.screens()[0]
        frame = screen.visibleFrame()
        height = self.window.frame().size.height
        self.window.setFrameOrigin_((
            frame.origin.x + (frame.size.width - WIDTH) / 2,
            frame.origin.y + (frame.size.height - height) / 2))

    # -- tables ------------------------------------------------------------

    def numberOfRowsInTableView_(self, table):
        # AppKit queries the data source the moment it is attached, which is
        # while the panes are still being built.
        if table is getattr(self, "profile_table", None):
            return len(self.profile_rows())
        return device.GKEY_COUNT

    def tableView_objectValueForTableColumn_row_(self, table, column, row):
        if table is getattr(self, "profile_table", None):
            rows = self.profile_rows()
            if row >= len(rows):
                return ""
            name, binding = rows[row]
            return name if column.identifier() == "key" else actions.describe(binding)
        name = f"G{row + 1}"
        if column.identifier() == "key":
            return name
        binding = self.config.get("bindings", {}).get(name)
        return actions.describe(binding) if binding else "—"

    # -- actions -----------------------------------------------------------

    @objc.IBAction
    def paneChanged_(self, sender):
        self.show_pane(PANES[int(sender.selectedSegment())])

    @objc.IBAction
    def presetClicked_(self, sender):
        self.apply_color(config.NAMED_COLORS[str(sender.identifier())])

    @objc.IBAction
    def colorChanged_(self, sender):
        colour = sender.color().colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
        if colour is None:
            return
        self.apply_color((int(colour.redComponent() * 255),
                          int(colour.greenComponent() * 255),
                          int(colour.blueComponent() * 255)))

    @objc.python_method
    def apply_color(self, rgb):
        brightness = self.config.get("brightness", 100)
        try:
            control.set_backlight(config.apply_brightness(rgb, brightness))
        except control.ControlError:
            pass
        self.mutate(lambda s: s.update(backlight=config.format_color(rgb)))
        self.refresh()

    @objc.IBAction
    def brightnessChanged_(self, sender):
        value = int(sender.intValue())
        self.brightness_label.setStringValue_(f"{value}% brightness")
        fresh = self.mutate(lambda s: s.update(brightness=value))
        try:
            control.set_backlight(config.effective_color(fresh))
        except control.ControlError:
            pass

    @objc.IBAction
    def switchChanged_(self, sender):
        key = str(sender.identifier())
        index = int(sender.indexOfSelectedItem())
        value = SWITCH_ACTIONS[index] if 0 <= index < len(SWITCH_ACTIONS) else "nothing"

        def apply(settings):
            block = settings.setdefault("game_switch", {})
            if value == "nothing":
                block.pop(key, None)
            else:
                block[key] = value

        self.mutate(apply)

    @objc.IBAction
    def screenChanged_(self, sender):
        index = int(sender.indexOfSelectedItem())
        names = list(screens.SCREENS)
        choice = names[index] if index < len(names) else "off"

        def apply(settings):
            lcd = settings.setdefault("lcd", {})
            lcd["enabled"] = choice != "off"
            if choice != "off":
                lcd["screen"] = choice

        self.mutate(apply)
        try:
            control.clear_lcd() if choice == "off" else control.show_screen(choice)
        except control.ControlError:
            pass
        self.refresh()

    @objc.IBAction
    def lcdKeyChanged_(self, sender):
        key = str(sender.identifier())
        index = int(sender.indexOfSelectedItem())
        names = list(screens.SCREENS)
        if index < len(names):
            value = f"screen:{names[index]}"
        else:
            value = ["next", "prev", "off"][index - len(names)]
        self.mutate(lambda s: s.setdefault("lcd_keys", {}).update({key: value}))

    @objc.IBAction
    def claudeRowChanged_(self, sender):
        keys = list(screens.CLAUDE_ROWS)
        chosen = []
        for popup in self.claude_popups:
            index = int(popup.indexOfSelectedItem())
            if index < len(keys) and keys[index] not in chosen:
                chosen.append(keys[index])
        self.mutate(lambda s: s.setdefault("claude", {}).update(rows=chosen))
        self.refresh()

    @objc.IBAction
    def printerToggled_(self, sender):
        wanted = bool(sender.state())
        self.mutate(lambda s: s.setdefault("printer", {}).update(enabled=wanted))
        self.refresh()

    @objc.IBAction
    def printerHostChanged_(self, sender):
        host = str(sender.stringValue()).strip()
        self.mutate(lambda s: s.setdefault("printer", {}).update(host=host))
        self.refresh()

    @objc.IBAction
    def printerTest_(self, _sender):
        self.printerHostChanged_(self.printer_host)
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
        self.refresh()

    @objc.IBAction
    def bankChanged_(self, sender):
        name = config.BANKS[int(sender.selectedSegment())]
        if control.daemon_running():
            try:
                ipc.request({"cmd": "set_bank", "bank": name})
            except OSError:
                pass
        else:
            settings = config.load()
            config.set_bank(settings, name)
            config.save(settings)
        self.refresh()

    @objc.IBAction
    def profileAppChanged_(self, sender):
        self.editing_app = str(sender.titleOfSelectedItem())
        self.profile_table.reloadData()

    @objc.IBAction
    def addProfileBinding_(self, _sender):
        if not self.editing_app:
            self.complain("No application to add an override for.")
            return
        self.prompt_binding("G1", {}, app=self.editing_app)

    @objc.IBAction
    def editProfileBinding_(self, _sender):
        row = self.profile_table.clickedRow()
        rows = self.profile_rows()
        if row < 0 or row >= len(rows):
            return
        name, binding = rows[row]
        self.prompt_binding(name, binding, app=self.editing_app)

    @objc.IBAction
    def editBinding_(self, _sender):
        row = self.table.clickedRow()
        if row < 0:
            return
        name = f"G{row + 1}"
        self.prompt_binding(name, self.config.get("bindings", {}).get(name) or {})

    @objc.python_method
    def prompt_binding(self, key, binding, app=None):
        """The shared editor for a binding, whether base or per-app."""
        self.editing_key = key
        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"{key} in {app}" if app else f"Binding for {key}")
        alert.setInformativeText_(
            f"Applies only while {app} is frontmost." if app
            else "Choose what this key should do.")
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")

        accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 110))
        keys_popup = None
        if app:
            keys_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(0, 78, 150, 26), False)
            for index in range(1, device.GKEY_COUNT + 1):
                keys_popup.addItemWithTitle_(f"G{index}")
            keys_popup.selectItemWithTitle_(key)
            accessory.addSubview_(keys_popup)

        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(0, 46, 150, 26), False)
        for name in BINDING_TYPES:
            popup.addItemWithTitle_(name)
        popup.selectItemWithTitle_(binding.get("type", "app"))
        popup.setTarget_(self)
        popup.setAction_(b"bindingTypeChanged:")
        accessory.addSubview_(popup)

        field = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 14, 320, 24))
        current = ""
        for name in ("name", "keys", "command", "text"):
            if name in binding:
                current = binding[name]
        field.setStringValue_(current)
        field.setPlaceholderString_(TYPE_HINTS.get(binding.get("type", "app"), ""))
        accessory.addSubview_(field)

        self.edit_field = field
        alert.setAccessoryView_(accessory)
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() != 1000:
            return
        if keys_popup is not None:
            self.editing_key = str(keys_popup.titleOfSelectedItem())
        self.save_binding(str(popup.titleOfSelectedItem()),
                          str(field.stringValue()).strip(), app=app)

    @objc.IBAction
    def bindingTypeChanged_(self, sender):
        self.edit_field.setPlaceholderString_(
            TYPE_HINTS.get(str(sender.titleOfSelectedItem()), ""))

    @objc.python_method
    def save_binding(self, kind, value, app=None):
        key = self.editing_key
        if kind != "none" and value and kind == "keys":
            try:
                actions.parse_chord(value)
            except actions.ActionError as exc:
                self.complain(str(exc))
                return

        def apply(settings):
            if app:
                profiles = settings.setdefault("app_profiles", {})
                target = profiles.setdefault(app, {})
            else:
                target = settings.setdefault("bindings", {})
            if kind == "none" or not value:
                target.pop(key, None)
                if app and not target:
                    settings["app_profiles"].pop(app, None)
            else:
                field = {"app": "name", "keys": "keys", "shell": "command",
                         "text": "text", "macro": "name"}[kind]
                target[key] = {"type": kind, field: value}

        self.mutate(apply)
        self.refresh()

    @objc.python_method
    def complain(self, message):
        alert = NSAlert.alloc().init()
        alert.setMessageText_("That did not work")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    @objc.IBAction
    def recordMacro_(self, _sender):
        row = self.table.selectedRow()
        if row < 0:
            self.complain("Select a G-key row first, then record.")
            return
        key = f"G{row + 1}"
        if not actions.can_post_events():
            self.complain("Recording needs Accessibility permission. "
                          "Use the Permissions button in the Agent tab.")
            return
        notice = NSAlert.alloc().init()
        notice.setMessageText_(f"Record a macro for {key}")
        notice.setInformativeText_(
            "After you click Start, type the sequence you want, then press "
            "Escape to finish.\n\nKeys still reach whatever app is focused, "
            "so switch to a safe window first if that matters.")
        notice.addButtonWithTitle_("Start")
        notice.addButtonWithTitle_("Cancel")
        if notice.runModal() != 1000:
            return
        self.window.miniaturize_(None)
        try:
            steps = recorder.record(timeout=120.0)
        except recorder.RecordingError as exc:
            self.window.deminiaturize_(None)
            self.complain(str(exc))
            return
        self.window.deminiaturize_(None)
        self.show()
        if not steps:
            self.complain("Nothing was recorded, so the binding is unchanged.")
            return
        name = key.lower()

        def apply(settings):
            settings.setdefault("macros", {})[name] = steps
            settings.setdefault("bindings", {})[key] = {
                "type": "macro", "name": name}

        self.mutate(apply)
        self.refresh()
        done = NSAlert.alloc().init()
        done.setMessageText_(f"{key} now plays a {len(steps)}-step macro")
        done.setInformativeText_(recorder.summarise(steps, limit=8))
        done.addButtonWithTitle_("OK")
        done.runModal()

    @objc.IBAction
    def toggleAgent_(self, _sender):
        self.invalidate_agent_cache()
        if cli.agent_loaded():
            subprocess.run(["launchctl", "unload", "-w", cli.PLIST_PATH],
                           capture_output=True)
        else:
            try:
                cli.cmd_start([])
            except SystemExit as exc:
                self.complain(str(exc))
        self.invalidate_agent_cache()
        self.refresh()

    @objc.IBAction
    def askPermissions_(self, _sender):
        if not actions.can_post_events():
            actions.request_post_access()
        subprocess.run(["open", "x-apple.systempreferences:com.apple."
                        "preference.security?Privacy_Accessibility"])
        self.refresh()

    @objc.IBAction
    def editConfig_(self, _sender):
        config.ensure_exists()
        NSWorkspace.sharedWorkspace().openFile_(config.CONFIG_PATH)

    @objc.IBAction
    def installCli_(self, _sender):
        try:
            path = cli.install_cli_tool()
        except OSError as exc:
            self.complain(str(exc))
            return
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Command line tool installed")
        on_path = os.path.dirname(path) in os.environ.get("PATH", "").split(":")
        alert.setInformativeText_(
            f"{path}\n\n" + ("Try: g510 --help" if on_path else
                              "That folder is not on your PATH yet. Add it to "
                              "~/.zshrc:\n\n"
                              'export PATH="$HOME/.local/bin:$PATH"'))
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    @objc.IBAction
    def openLog_(self, _sender):
        NSWorkspace.sharedWorkspace().openFile_(cli.LOG_PATH)
