"""Render text and graphics for the G510's 160x43 monochrome LCD.

Drawing goes through CoreGraphics/CoreText so we get real font rendering and
metrics rather than a hand-rolled bitmap font.
"""
import Quartz
from Quartz import CGBitmapContextCreate, CGColorSpaceCreateDeviceGray, CGRectMake

from device import LCD_HEIGHT, LCD_WIDTH

DEFAULT_FONT = b"Menlo-Bold"
THRESHOLD = 110          # grey level above which a pixel counts as lit


class Canvas:
    """A 160x43 1-bit drawing surface backed by a CoreGraphics context."""

    def __init__(self):
        colorspace = CGColorSpaceCreateDeviceGray()
        self.ctx = CGBitmapContextCreate(
            None, LCD_WIDTH, LCD_HEIGHT, 8, LCD_WIDTH, colorspace, 0)
        self.clear()

    def clear(self):
        Quartz.CGContextSetGrayFillColor(self.ctx, 0.0, 1.0)
        Quartz.CGContextFillRect(self.ctx, CGRectMake(0, 0, LCD_WIDTH, LCD_HEIGHT))

    def text(self, string, x, y, size=12.0, font=DEFAULT_FONT):
        """Draw a string with its baseline at (x, y), measured from the top."""
        Quartz.CGContextSetGrayFillColor(self.ctx, 1.0, 1.0)
        Quartz.CGContextSelectFont(self.ctx, font, size, Quartz.kCGEncodingMacRoman)
        Quartz.CGContextSetTextDrawingMode(self.ctx, Quartz.kCGTextFill)
        encoded = string.encode("mac_roman", "replace")
        Quartz.CGContextShowTextAtPoint(
            self.ctx, x, LCD_HEIGHT - y - size, encoded, len(encoded))

    def centered(self, string, y, size=12.0, font=DEFAULT_FONT):
        """Draw a string horizontally centred, baseline y from the top."""
        width = self.measure(string, size, font)
        self.text(string, max(0, (LCD_WIDTH - width) / 2), y, size, font)

    def measure(self, string, size=12.0, font=DEFAULT_FONT):
        """Width in pixels the string will occupy."""
        Quartz.CGContextSelectFont(self.ctx, font, size, Quartz.kCGEncodingMacRoman)
        Quartz.CGContextSetTextDrawingMode(self.ctx, Quartz.kCGTextInvisible)
        encoded = string.encode("mac_roman", "replace")
        Quartz.CGContextShowTextAtPoint(self.ctx, 0, 0, encoded, len(encoded))
        end = Quartz.CGContextGetTextPosition(self.ctx)
        Quartz.CGContextSetTextDrawingMode(self.ctx, Quartz.kCGTextFill)
        return end.x

    def rect(self, x, y, width, height, filled=True):
        Quartz.CGContextSetGrayFillColor(self.ctx, 1.0, 1.0)
        Quartz.CGContextSetGrayStrokeColor(self.ctx, 1.0, 1.0)
        box = CGRectMake(x, LCD_HEIGHT - y - height, width, height)
        if filled:
            Quartz.CGContextFillRect(self.ctx, box)
        else:
            Quartz.CGContextSetLineWidth(self.ctx, 1.0)
            Quartz.CGContextStrokeRect(self.ctx, box)

    def bar(self, x, y, width, height, fraction):
        """An outlined progress bar filled to `fraction` (0..1).

        The fill is inset by two on every side, so on a short bar the height
        has to be floored or there is nothing left to draw.
        """
        self.rect(x, y, width, height, filled=False)
        inner = max(0, min(1.0, fraction)) * (width - 4)
        if inner >= 1:
            self.rect(x + 2, y + 2, int(inner), max(1, height - 4), filled=True)

    def preview(self, scale=2):
        """An NSImage of the current canvas, for showing the LCD on screen."""
        from AppKit import NSBitmapImageRep, NSImage, NSMakeSize
        rows = self.pixels()
        width, height = LCD_WIDTH * scale, LCD_HEIGHT * scale
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, width, height, 8, 4, True, False, "NSDeviceRGBColorSpace",
            width * 4, 32)
        data = rep.bitmapData()
        # An amber-on-dark panel, matching how the real display reads.
        lit = (255, 190, 60, 255)
        dark = (28, 26, 22, 255)
        buffer = bytearray(width * height * 4)
        for y in range(height):
            source = rows[y // scale]
            base = y * width * 4
            for x in range(width):
                colour = lit if source[x // scale] else dark
                offset = base + x * 4
                buffer[offset:offset + 4] = bytes(colour)
        data[:] = bytes(buffer)
        image = NSImage.alloc().initWithSize_(NSMakeSize(width, height))
        image.addRepresentation_(rep)
        return image

    def pixels(self):
        """Threshold the canvas into rows of 0/1 ready for device.pack_lcd."""
        raw = Quartz.CGBitmapContextGetData(self.ctx).as_buffer(
            LCD_WIDTH * LCD_HEIGHT)[:]
        return [[1 if raw[y * LCD_WIDTH + x] > THRESHOLD else 0
                 for x in range(LCD_WIDTH)]
                for y in range(LCD_HEIGHT)]
