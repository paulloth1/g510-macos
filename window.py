"""The G510 window, laid out the way a Mac settings window is.

Grouped cards on a flat background, secondary text for detail, SF Symbols where
they carry meaning, and a live preview of what the keyboard's LCD is showing.
"""
import subprocess

import objc
from AppKit import (NSAlert, NSApplication, NSBackingStoreBuffered,
                    NSBezelStyleRounded, NSBox, NSButton, NSColor, NSColorWell,
                    NSFont, NSImage, NSImageView, NSMakeRect, NSMakeSize,
                    NSMakePoint, NSPopUpButton, NSScreen, NSScrollView,
                    NSSegmentedControl,
                    NSSlider, NSTableColumn, NSTableView, NSTextField, NSView,
                    NSWindow, NSWindowStyleMaskClosable,
                    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled, NSWorkspace)
from Foundation import NSObject, NSTimer

import actions
import cli
import config
import control
import device
import recorder
import screens
from lcd import Canvas

WIDTH = 520
# Total height the cards occupy. The window opens shorter than this and the
# content scrolls, so it still fits on a small display.
CONTENT_HEIGHT = 948
MIN_HEIGHT = 400
DEFAULT_HEIGHT = 760
MARGIN = 20
CARD_WIDTH = WIDTH - MARGIN * 2

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

NSBoxCustom = 4
NSNoTitle = 0
NSTableViewStyleInset = 2
NSLineBreakByTruncatingTail = 4


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


class G510Window(NSObject):

    def initWithMenu_(self, menu):
        self = objc.super(G510Window, self).init()
        if self is None:
            return None
        self.menu = menu
        self.config = config.load()
        self.editing_key = None
        self.build()
        self.start_timer()
        return self

    # -- construction ------------------------------------------------------

    @objc.python_method
    def card(self, parent, y_from_top, height, title=None):
        """A rounded settings-style group, returned with its content view."""
        y = CONTENT_HEIGHT - y_from_top - height
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
            place(header, MARGIN, CONTENT_HEIGHT - y_from_top + 4, 300, 15)
            parent.addSubview_(header)
        return box.contentView(), height

    @objc.python_method
    def build(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        height = self.fitting_height()
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, height), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("G510")
        self.window.setReleasedWhenClosed_(False)
        self.window.setContentMinSize_(NSMakeSize(WIDTH, MIN_HEIGHT))
        self.window.setContentMaxSize_(NSMakeSize(WIDTH, CONTENT_HEIGHT))

        self.scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH, height))
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setDrawsBackground_(False)
        self.scroll.setBorderType_(0)
        self.scroll.setAutoresizingMask_(2 | 16)      # width and height sizable
        root = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH, CONTENT_HEIGHT))
        self.scroll.setDocumentView_(root)
        self.window.contentView().addSubview_(self.scroll)
        self.center_on_active_screen()

        cursor = MARGIN

        # -- device -------------------------------------------------------
        content, height = self.card(root, cursor, 62)
        icon = NSImageView.alloc().init()
        icon.setImage_(symbol("keyboard", 26))
        icon.setContentTintColor_(NSColor.secondaryLabelColor())
        content.addSubview_(place(icon, 16, height - 42, 30, 26))
        self.device_title = text("G510 Gaming Keyboard", 13, bold=True)
        content.addSubview_(place(self.device_title, 56, height - 36, 380, 18))
        self.device_detail = text("", 11, secondary=True)
        content.addSubview_(place(self.device_detail, 56, height - 54, 380, 15))
        cursor += height + 24

        # -- backlight ----------------------------------------------------
        content, height = self.card(root, cursor, 110, "BACKLIGHT")
        self.color_well = NSColorWell.alloc().initWithFrame_(
            NSMakeRect(16, height - 44, 56, 28))
        self.color_well.setTarget_(self)
        self.color_well.setAction_(b"colorChanged:")
        content.addSubview_(self.color_well)
        self.color_label = text("", 12)
        content.addSubview_(place(self.color_label, 84, height - 38, 120, 17))
        self.brightness_slider = NSSlider.alloc().initWithFrame_(
            NSMakeRect(84, height - 60, 250, 20))
        self.brightness_slider.setMinValue_(0)
        self.brightness_slider.setMaxValue_(100)
        self.brightness_slider.setTarget_(self)
        self.brightness_slider.setAction_(b"brightnessChanged:")
        self.brightness_slider.setContinuous_(True)
        content.addSubview_(self.brightness_slider)
        self.brightness_label = text("", 11, secondary=True)
        content.addSubview_(place(self.brightness_label, 342, height - 58, 90, 15))

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
        cursor += height + 24

        # -- display ------------------------------------------------------
        content, height = self.card(root, cursor, 222, "DISPLAY")
        self.screen_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(16, height - 44, 170, 26), False)
        for name in list(screens.SCREENS) + ["off"]:
            self.screen_popup.addItemWithTitle_(name.title())
        self.screen_popup.setTarget_(self)
        self.screen_popup.setAction_(b"screenChanged:")
        content.addSubview_(self.screen_popup)
        self.showing_label = text("", 11, secondary=True)
        content.addSubview_(place(self.showing_label, 196, height - 38, 270, 15))

        buttons_title = text("Buttons under the screen, left to right",
                             11, secondary=True)
        content.addSubview_(place(buttons_title, 16, 50, 300, 15))
        self.lcd_key_popups = {}
        x = 14
        for key in ("L2", "L3", "L4", "L5"):
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(x, 14, 110, 24), False)
            for name in list(screens.SCREENS) + ["next", "prev", "off"]:
                popup.addItemWithTitle_(name.title())
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
        # The panel render is deliberately fixed amber-on-dark; a hairline
        # keeps it reading as a screen rather than a hole on a light theme.
        layer.setBorderWidth_(1.0)
        layer.setBorderColor_(NSColor.separatorColor().CGColor())
        content.addSubview_(self.preview)
        cursor += height + 24

        # -- profiles -----------------------------------------------------
        content, height = self.card(root, cursor, 56, "PER-APP PROFILE")
        self.profile_label = text("", 12)
        content.addSubview_(place(self.profile_label, 16, height - 36, 330, 17))
        self.profile_detail = text("", 11, secondary=True)
        content.addSubview_(place(self.profile_detail, 16, height - 52, 330, 15))
        cursor += height + 24

        # -- g-keys -------------------------------------------------------
        content, height = self.card(root, cursor, 254, "G-KEY BINDINGS")
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(1, 30, CARD_WIDTH - 2, height - 61))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        scroll.setDrawsBackground_(False)
        self.table = NSTableView.alloc().init()
        for identifier, title_text, width in (("key", "Key", 56),
                                              ("action", "Action", 392)):
            column = NSTableColumn.alloc().initWithIdentifier_(identifier)
            column.headerCell().setStringValue_(title_text)
            column.setWidth_(width)
            self.table.addTableColumn_(column)
        try:
            self.table.setStyle_(NSTableViewStyleInset)
        except AttributeError:
            pass
        self.table.setRowHeight_(22.0)
        self.table.setUsesAlternatingRowBackgroundColors_(False)
        self.table.setGridStyleMask_(0)
        self.table.setBackgroundColor_(NSColor.clearColor())
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        self.table.setTarget_(self)
        self.table.setDoubleAction_(b"editBinding:")
        scroll.setDocumentView_(self.table)
        content.addSubview_(scroll)

        self.bank_picker = NSSegmentedControl.alloc().initWithFrame_(
            NSMakeRect(CARD_WIDTH - 152, height - 26, 138, 22))
        self.bank_picker.setSegmentCount_(3)
        for index, name in enumerate(config.BANKS):
            self.bank_picker.setLabel_forSegment_(f"M{name}", index)
            self.bank_picker.setWidth_forSegment_(46, index)
        self.bank_picker.setTarget_(self)
        self.bank_picker.setAction_(b"bankChanged:")
        content.addSubview_(self.bank_picker)

        footer = text("Double-click a row to change it", 11, secondary=True)
        content.addSubview_(place(footer, 14, 8, 200, 15))
        record_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(CARD_WIDTH - 268, 5, 116, 22))
        record_button.setTitle_("Record macro…")
        record_button.setBezelStyle_(NSBezelStyleRounded)
        record_button.setFont_(NSFont.systemFontOfSize_(11))
        record_button.setTarget_(self)
        record_button.setAction_(b"recordMacro:")
        content.addSubview_(record_button)
        edit = NSButton.alloc().initWithFrame_(
            NSMakeRect(CARD_WIDTH - 148, 5, 134, 22))
        edit.setTitle_("Edit JSON…")
        edit.setBezelStyle_(NSBezelStyleRounded)
        edit.setFont_(NSFont.systemFontOfSize_(11))
        edit.setTarget_(self)
        edit.setAction_(b"editConfig:")
        content.addSubview_(edit)
        cursor += height + 24

        # -- agent --------------------------------------------------------
        content, height = self.card(root, cursor, 84, "BACKGROUND AGENT")
        self.agent_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(16, height - 46, 130, 30))
        self.agent_button.setBezelStyle_(NSBezelStyleRounded)
        self.agent_button.setTarget_(self)
        self.agent_button.setAction_(b"toggleAgent:")
        content.addSubview_(self.agent_button)
        self.agent_label = text("", 11, secondary=True)
        content.addSubview_(place(self.agent_label, 156, height - 40, 330, 15))
        self.permission_label = text("", 11, secondary=True)
        content.addSubview_(place(self.permission_label, 16, 14, 340, 15))
        self.permission_button = NSButton.alloc().initWithFrame_(
            NSMakeRect(CARD_WIDTH - 148, 10, 134, 22))
        self.permission_button.setTitle_("Permissions…")
        self.permission_button.setBezelStyle_(NSBezelStyleRounded)
        self.permission_button.setFont_(NSFont.systemFontOfSize_(11))
        self.permission_button.setTarget_(self)
        self.permission_button.setAction_(b"askPermissions:")
        content.addSubview_(self.permission_button)

        self.refresh()
        self.scroll_to_top()

    @objc.python_method
    def fitting_height(self):
        """As much of the content as the screen will comfortably take."""
        screen = NSScreen.mainScreen() or NSScreen.screens()[0]
        usable = int(screen.visibleFrame().size.height) - 80
        return max(MIN_HEIGHT, min(CONTENT_HEIGHT, DEFAULT_HEIGHT, usable))

    @objc.python_method
    def scroll_to_top(self):
        clip = self.scroll.contentView()
        clip.scrollToPoint_(NSMakePoint(
            0, CONTENT_HEIGHT - clip.bounds().size.height))
        self.scroll.reflectScrolledClipView_(clip)

    @objc.python_method
    def center_on_active_screen(self):
        """Put the window on the screen being used, not a secondary display."""
        screen = NSScreen.mainScreen() or NSScreen.screens()[0]
        frame = screen.visibleFrame()
        height = self.window.frame().size.height
        self.window.setFrameOrigin_((
            frame.origin.x + (frame.size.width - WIDTH) / 2,
            frame.origin.y + (frame.size.height - height) / 2))

    # -- state -------------------------------------------------------------

    @objc.python_method
    def start_timer(self):
        """Keep the window honest about changes made elsewhere."""
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, b"tick:", None, True)

    def tick_(self, _timer):
        """Keep the window honest: the keyboard is driven from elsewhere too."""
        if not self.window.isVisible():
            return
        try:
            self.refresh()
        except Exception:
            self.refresh_preview()

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
        connected = control.present()
        self.device_title.setStringValue_(
            "G510 Gaming Keyboard" if connected else "No G510 found")
        if connected:
            detail = "Connected"
            if control.daemon_running():
                detail += " · agent holds the device"
            self.device_detail.setTextColor_(NSColor.secondaryLabelColor())
        else:
            detail = "Plug the keyboard in, or check the dock"
            self.device_detail.setTextColor_(NSColor.systemRedColor())
        self.device_detail.setStringValue_(detail)

        brightness = int(self.config.get("brightness", 100))
        self.brightness_slider.setIntValue_(brightness)
        self.brightness_label.setStringValue_(f"{brightness}% brightness")

        app = screens.frontmost_app()
        profile = (self.config.get("app_profiles") or {}).get(app)
        if profile:
            self.profile_label.setStringValue_(f"{app}")
            self.profile_detail.setStringValue_(
                f"{len(profile)} override" + ("s" if len(profile) != 1 else "")
                + ": " + ", ".join(sorted(profile, key=lambda k: int(k[1:]))))
        else:
            self.profile_label.setStringValue_(app or "No frontmost app")
            self.profile_detail.setStringValue_(
                "No overrides for this app - set them with g510 profile")

        try:
            rgb = control.get_backlight()
            self.color_well.setColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, 1.0))
            self.color_label.setStringValue_(
                config.format_color(config.parse_color(
                    self.config.get("backlight", "#ffffff"))))
        except control.ControlError:
            self.color_label.setStringValue_("-")

        lcd = self.config.get("lcd", {})
        active = lcd.get("screen", "status") if lcd.get("enabled", True) else "off"
        self.screen_popup.selectItemWithTitle_(active.title())
        self.showing_label.setStringValue_(
            "showing now" if lcd.get("enabled", True) else "display is blank")

        lcd_keys = self.config.get("lcd_keys") or {}
        for key, popup in self.lcd_key_popups.items():
            value = lcd_keys.get(key) or "next"
            title = (value.split(":", 1)[1] if isinstance(value, str)
                     and value.startswith("screen:") else value)
            if isinstance(title, str):
                popup.selectItemWithTitle_(title.title())
        self.refresh_preview()

        active = str(self.config.get("active_bank", "1"))
        if active in config.BANKS:
            self.bank_picker.setSelectedSegment_(config.BANKS.index(active))

        running = cli.agent_loaded()
        self.agent_button.setTitle_("Stop" if running else "Start")
        self.agent_label.setStringValue_(
            "Running, and starts at login" if running
            else "Not running - G-keys will not do anything")

        posting = actions.can_post_events()
        reading = actions.can_read_input()
        if posting and reading:
            self.permission_label.setTextColor_(NSColor.secondaryLabelColor())
            self.permission_label.setStringValue_(
                "Input Monitoring and Accessibility granted")
            self.permission_button.setHidden_(True)
        else:
            missing = []
            if not reading:
                missing.append("Input Monitoring")
            if not posting:
                missing.append("Accessibility")
            self.permission_label.setTextColor_(NSColor.systemOrangeColor())
            self.permission_label.setStringValue_(f"Needed: {', '.join(missing)}")
            self.permission_button.setHidden_(False)
        self.table.reloadData()

    @objc.python_method
    def mutate(self, apply):
        """Re-read, change, write back.

        The agent and the CLI write this file too, and the window holds its
        copy for up to a second between refreshes. Saving that copy wholesale
        would revert anything they changed in the meantime.
        """
        fresh = config.load()
        apply(fresh)
        config.save(fresh)
        self.config = fresh
        cli.reload_agent()
        if self.menu is not None:
            self.menu.rebuild()
        return fresh

    @objc.python_method
    def push_config(self):
        self.mutate(lambda settings: None)

    @objc.python_method
    def show(self):
        self.center_on_active_screen()
        self.scroll_to_top()
        self.window.makeKeyAndOrderFront_(None)
        self.window.orderFrontRegardless()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.refresh()

    # -- table -------------------------------------------------------------

    def numberOfRowsInTableView_(self, _table):
        return device.GKEY_COUNT

    def tableView_objectValueForTableColumn_row_(self, _table, column, row):
        name = f"G{row + 1}"
        if column.identifier() == "key":
            return name
        binding = self.config.get("bindings", {}).get(name)
        return actions.describe(binding) if binding else "—"

    # -- actions -----------------------------------------------------------

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
        try:
            control.set_backlight(rgb)
        except control.ControlError:
            pass
        self.mutate(lambda s: s.update(backlight=config.format_color(rgb)))
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
    def lcdKeyChanged_(self, sender):
        key = str(sender.identifier())
        choice = str(sender.titleOfSelectedItem()).lower()
        value = choice if choice in ("next", "prev", "off") else f"screen:{choice}"
        self.mutate(lambda s: s.setdefault("lcd_keys", {}).update({key: value}))

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
    def recordMacro_(self, _sender):
        row = self.table.selectedRow()
        if row < 0:
            self.complain("Select a G-key row first, then record.")
            return
        key = f"G{row + 1}"
        if not actions.can_post_events():
            self.complain("Recording needs Accessibility permission. "
                          "Use the Permissions button below.")
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
    def screenChanged_(self, sender):
        choice = str(sender.titleOfSelectedItem()).lower()

        def apply(settings):
            lcd = settings.setdefault("lcd", {})
            lcd["enabled"] = choice != "off"
            if choice != "off":
                lcd["screen"] = choice

        self.mutate(apply)
        try:
            if choice == "off":
                control.clear_lcd()
            else:
                control.show_screen(choice)
        except control.ControlError:
            pass
        self.refresh()

    @objc.IBAction
    def editBinding_(self, _sender):
        row = self.table.clickedRow()
        if row < 0:
            return
        self.editing_key = f"G{row + 1}"
        binding = self.config.get("bindings", {}).get(self.editing_key) or {}

        alert = NSAlert.alloc().init()
        alert.setMessageText_(f"Binding for {self.editing_key}")
        alert.setInformativeText_("Choose what this key should do.")
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")

        accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 320, 78))
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
        for key in ("name", "keys", "command", "text"):
            if key in binding:
                current = binding[key]
        field.setStringValue_(current)
        field.setPlaceholderString_(TYPE_HINTS.get(binding.get("type", "app"), ""))
        accessory.addSubview_(field)

        self.edit_popup, self.edit_field = popup, field
        alert.setAccessoryView_(accessory)
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() == 1000:          # Save
            self.save_binding(str(popup.titleOfSelectedItem()),
                              str(field.stringValue()).strip())

    @objc.IBAction
    def bindingTypeChanged_(self, sender):
        self.edit_field.setPlaceholderString_(
            TYPE_HINTS.get(str(sender.titleOfSelectedItem()), ""))

    @objc.python_method
    def save_binding(self, kind, value):
        key = self.editing_key
        if kind != "none" and value and kind == "keys":
            try:
                actions.parse_chord(value)
            except actions.ActionError as exc:
                self.complain(str(exc))
                return

        def apply(settings):
            bindings = settings.setdefault("bindings", {})
            if kind == "none" or not value:
                bindings.pop(key, None)
            else:
                field = {"app": "name", "keys": "keys", "shell": "command",
                         "text": "text", "macro": "name"}[kind]
                bindings[key] = {"type": kind, field: value}

        self.mutate(apply)
        self.refresh()

    @objc.python_method
    def complain(self, message):
        alert = NSAlert.alloc().init()
        alert.setMessageText_("That binding is not valid")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK")
        alert.runModal()

    @objc.IBAction
    def toggleAgent_(self, _sender):
        if cli.agent_loaded():
            subprocess.run(["launchctl", "unload", "-w", cli.PLIST_PATH],
                           capture_output=True)
        else:
            try:
                cli.cmd_start([])
            except SystemExit:
                pass
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
