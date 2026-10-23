"""
Monitor Mirror - press a hotkey to see a live copy of another monitor.

  Ctrl+Alt+M         toggle the mirror on/off
  Ctrl+Alt+C         capture / release the cursor (it moves to the mirrored
                     monitor and stays there, so you can work it from the mirror)
  Click the mirror   capture the cursor at that spot
  Esc / X button     hide it (the toolbar appears when you hover over the mirror)
  Drag               move the picture-in-picture window
  Scroll wheel       resize the picture-in-picture window
  Tray icon          right-click for settings, left-click to toggle

Setup:
  pip install mss pillow pystray "dxcam[cv2]"
  Double-click mirror.pyw (the .pyw extension runs it with no console window)

Capture runs on the GPU (Windows Desktop Duplication) and only does work when
the mirrored screen or the cursor actually changes. If GPU capture is unavailable
it falls back to the older GDI method automatically.

Settings are saved to %APPDATA%\\MonitorMirror\\settings.json
"""
import argparse
import atexit
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import messagebox

import cv2
import mss
import numpy as np
import pystray
from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk

try:
    import dxcam
    HAVE_DXCAM = True
except Exception:          # not installed, or no Desktop Duplication on this machine
    dxcam = None
    HAVE_DXCAM = False

APP_NAME = "Monitor Mirror"
APP_VERSION = "2.2"
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
HOTKEYS = {
    1: ("Ctrl+Alt+M", MOD_CONTROL | MOD_ALT, ord("M"), "toggle"),
    2: ("Ctrl+Alt+C", MOD_CONTROL | MOD_ALT, ord("C"), "capture"),
}
TOGGLE_KEY, CAPTURE_KEY = HOTKEYS[1][0], HOTKEYS[2][0]

SETTINGS_VERSION = 5
DEFAULTS = {
    "version": SETTINGS_VERSION,
    "source": 4,               # monitor to mirror
    "target": 1,               # monitor to show it on
    "mode": "fullscreen",      # "fullscreen" or "pip"
    "pip_scale": 0.40,
    "pip_corner": "top-right",
    "pip_pos": None,           # [x, y] once the window has been dragged
    "fps": 30,
    "engine": "gpu",           # "gpu" (Desktop Duplication) or "gdi"
    "show_cursor": True,
    "cursor_style": "real",    # "real" (actual Windows cursor) or "arrow"
    "cursor_tint": True,       # recolour the real cursor with the chosen colour
    "cursor_color": "blue",
    "cursor_size": 1.3,
    "cursor_glow": False,
    "capture_on_open": False,
    "edge_release": 2,
    "center_on_close": True,   # closing the mirror puts the cursor in the middle of its screen         # bump a screen edge this many times to release (0 = off)
}

SCALES = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80]
FPS_OPTIONS = [5, 10, 15, 20, 24, 30, 45, 60]
CURSOR_COLORS = {
    "blue": ("Blue", (59, 130, 246)),
    "white": ("White", (255, 255, 255)),
    "red": ("Red", (239, 68, 68)),
    "green": ("Green", (34, 197, 94)),
    "cyan": ("Cyan", (6, 182, 212)),
    "magenta": ("Magenta", (217, 70, 239)),
    "yellow": ("Yellow", (250, 204, 21)),
    "black": ("Black", (12, 12, 12)),
}
EDGE_BUMP_WINDOW = 1.5      # seconds the bumps must happen within
EDGE_REARM_PX = 12          # move this far off the edge before the next bump counts
CURSOR_SIZES = [("Normal", 1.3), ("Large", 1.8), ("Extra large", 2.4)]

BAR_H = 34
BAR_BG, BAR_FG = "#1f2937", "#e5e7eb"
BTN_HOVER, CLOSE_HOVER = "#374151", "#dc2626"
BORDER_W = 4

SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "MonitorMirror" / "settings.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

WM_HOTKEY = 0x0312
WM_CLOSE = 0x0010
CURSOR_SHOWING = 0x1
DI_NORMAL = 0x0003


# ----------------------------------------------------------------- colours

def luminance(rgb):
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def hex_color(rgb):
    return "#%02x%02x%02x" % tuple(int(max(0, min(255, c))) for c in rgb)


def accent_palette(rgb):
    """Border, captured-toolbar background and text colours derived from the chosen colour."""
    bar = (34, 34, 34) if luminance(rgb) < 40 else tuple(c * 0.45 for c in rgb)
    fg = (255, 255, 255) if luminance(bar) < 140 else (0, 0, 0)
    return hex_color(rgb), hex_color(bar), hex_color(fg)


# ----------------------------------------------------------------- settings

def load_settings():
    s = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_PATH.read_text())
        if saved.get("version", 0) < 3:          # pre-v3 cursor look: drop old cursor prefs
            for k in ("cursor_color", "cursor_glow"):
                saved.pop(k, None)
        saved.pop("cursor_ring", None)
        if saved.get("version", 0) < 5:          # v2.0 shipped 3 bumps as the default; now 2
            saved.pop("edge_release", None)
        s.update(saved)
    except Exception:
        pass
    s["version"] = SETTINGS_VERSION
    if s["cursor_color"] not in CURSOR_COLORS:
        s["cursor_color"] = DEFAULTS["cursor_color"]
    return s


def save_settings(s):
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def startup_command():
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}"'


def startup_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ----------------------------------------------------------------- win32 plumbing

class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


if os.name == "nt":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    # explicit prototypes: 64-bit handles get truncated without them
    def _proto(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)

    W = wintypes
    _proto(kernel32.CreateMutexW, W.HANDLE, ctypes.c_void_p, W.BOOL, W.LPCWSTR)
    _proto(user32.GetCursorInfo, W.BOOL, ctypes.POINTER(CURSORINFO))
    _proto(user32.SetCursorPos, W.BOOL, ctypes.c_int, ctypes.c_int)
    _proto(user32.ClipCursor, W.BOOL, ctypes.POINTER(W.RECT))
    _proto(user32.CopyIcon, W.HICON, W.HICON)
    _proto(user32.DestroyIcon, W.BOOL, W.HICON)
    _proto(user32.GetIconInfo, W.BOOL, W.HICON, ctypes.POINTER(ICONINFO))
    _proto(user32.DrawIconEx, W.BOOL, W.HDC, ctypes.c_int, ctypes.c_int, W.HICON,
           ctypes.c_int, ctypes.c_int, W.UINT, W.HBRUSH, W.UINT)
    _proto(user32.GetDC, W.HDC, W.HWND)
    _proto(user32.ReleaseDC, ctypes.c_int, W.HWND, W.HDC)
    _proto(user32.RegisterHotKey, W.BOOL, W.HWND, ctypes.c_int, W.UINT, W.UINT)
    _proto(user32.UnregisterHotKey, W.BOOL, W.HWND, ctypes.c_int)
    _proto(user32.GetMessageW, W.BOOL, ctypes.POINTER(W.MSG), W.HWND, W.UINT, W.UINT)
    _proto(gdi32.CreateCompatibleDC, W.HDC, W.HDC)
    _proto(gdi32.CreateDIBSection, W.HBITMAP, W.HDC, ctypes.POINTER(BITMAPINFO), W.UINT,
           ctypes.POINTER(ctypes.c_void_p), W.HANDLE, W.DWORD)
    _proto(gdi32.SelectObject, W.HGDIOBJ, W.HDC, W.HGDIOBJ)
    _proto(gdi32.DeleteObject, W.BOOL, W.HGDIOBJ)
    _proto(gdi32.DeleteDC, W.BOOL, W.HDC)
    _proto(gdi32.GetObjectW, ctypes.c_int, W.HANDLE, ctypes.c_int, ctypes.c_void_p)
    _proto(kernel32.CloseHandle, W.BOOL, W.HANDLE)
    _proto(user32.FindWindowW, W.HWND, W.LPCWSTR, W.LPCWSTR)
    _proto(user32.PostMessageW, W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
    _proto(user32.MessageBoxW, ctypes.c_int, W.HWND, W.LPCWSTR, W.LPCWSTR, W.UINT)
else:
    kernel32 = user32 = gdi32 = None


def make_dpi_aware():
    """So monitor and cursor coordinates are real pixels, not scaled ones."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_instance_mutex = None
MUTEX_NAME = "Local\\MonitorMirrorSingleInstance"
INSTANCE_WINDOW_TITLE = "MonitorMirror-Instance"   # hidden Tk root; lets a newer launch find us


def acquire_single_instance():
    """Become the only running copy. A newer launch asks the running copy to quit and
    takes over, so opening an updated file always runs the update.
    Returns False only if an older copy that doesn't support this is still running."""
    global _instance_mutex
    asked = False
    for _ in range(50):                       # up to ~5 seconds
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.get_last_error() != 183:    # ERROR_ALREADY_EXISTS
            _instance_mutex = handle
            return True
        kernel32.CloseHandle(handle)
        if not asked:
            hwnd = user32.FindWindowW("TkTopLevel", INSTANCE_WINDOW_TITLE)
            if not hwnd:
                return False
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            asked = True
        time.sleep(0.1)
    return False


def cursor_info():
    """(x, y, cursor handle) of the real cursor, or None when it's hidden."""
    info = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    if user32.GetCursorInfo(ctypes.byref(info)) and info.flags & CURSOR_SHOWING:
        return info.ptScreenPos.x, info.ptScreenPos.y, info.hCursor or 0
    return None


def cursor_position():
    info = cursor_info()
    return (info[0], info[1]) if info else None


def clip_cursor(m):
    """Confine the real cursor to monitor m (None releases it)."""
    if m is None:
        user32.ClipCursor(None)
    else:
        r = wintypes.RECT(m["left"], m["top"], m["left"] + m["width"], m["top"] + m["height"])
        user32.ClipCursor(ctypes.byref(r))


def hotkey_listener(app):
    """Registers the system-wide hotkeys and reports presses to the UI thread."""
    failed = []
    for hid, (label, mods, key, action) in HOTKEYS.items():
        if not user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, key):
            failed.append(label)
            if action == "capture":
                app.capture_available = False
    if failed:
        app.events.put(("hotkey-failed", failed))
    msg = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) not in (0, -1):
            if msg.message == WM_HOTKEY and msg.wParam in HOTKEYS:
                action = HOTKEYS[msg.wParam][3]
                app.post(app.mirror.toggle if action == "toggle" else app.mirror.toggle_capture)
    finally:
        for hid in HOTKEYS:
            user32.UnregisterHotKey(None, hid)


# ----------------------------------------------------------------- screen capture

def dxgi_output_for(mon):
    """(device_idx, output_idx) of the DXGI output covering this monitor, or None."""
    factory = vars(dxcam).get("__factory") if dxcam else None
    if factory is None:
        return None
    for d, outputs in enumerate(factory.outputs):
        for o, out in enumerate(outputs):
            try:
                out.update_desc()
                r = out.desc.DesktopCoordinates
            except Exception:
                continue
            if (r.left, r.top) == (mon["left"], mon["top"]):
                return d, o
    return None


class Capturer:
    """GPU capture via Desktop Duplication (only returns a frame when the screen changed),
    with GDI capture as a fallback. Frames are RGB numpy arrays."""

    def __init__(self, app):
        self.app = app
        self.sct = mss.mss()
        self.cam = None
        self.cam_key = None
        self.last = None              # last full-resolution frame
        self.active = "gdi"           # what actually produced the last frame
        self.retry_at = 0.0

    def _want_gpu(self):
        return HAVE_DXCAM and self.app.settings["engine"] == "gpu"

    def reset(self):
        """Call when the source monitor or engine changes."""
        self._release_cam()
        self.last = None
        self.retry_at = 0.0

    def _release_cam(self):
        if self.cam is not None:
            try:
                self.cam.release()
            except Exception:
                pass
        self.cam, self.cam_key = None, None

    def _ensure_cam(self, src):
        key = (src["left"], src["top"], src["width"], src["height"])
        if self.cam is not None and self.cam_key == key:
            return True
        if time.monotonic() < self.retry_at:
            return False
        self._release_cam()
        idx = dxgi_output_for(src)
        if idx is None:
            self.retry_at = time.monotonic() + 5
            return False
        try:
            self.cam = dxcam.create(device_idx=idx[0], output_idx=idx[1],
                                    output_color="RGB", processor_backend="cv2")
            self.cam_key = key
            return True
        except Exception:
            self.retry_at = time.monotonic() + 5
            return False

    def grab(self, src, need_frame=False):
        """New frame, or None if the screen hasn't changed (GPU path only).
        need_frame=True always returns something (the last frame if nothing changed)."""
        if self._want_gpu() and self._ensure_cam(src):
            try:
                frame = self.cam.grab()
                self.active = "gpu"
                if frame is not None:
                    self.last = frame
                    return frame
                if not need_frame:
                    return None
                if self.last is not None:
                    return self.last
                # nothing captured yet on this camera: fall through for one GDI frame
            except Exception:
                self._release_cam()
                self.retry_at = time.monotonic() + 2
        shot = self.sct.grab(src)
        self.last = cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2RGB)
        if not self._want_gpu() or self.cam is None:
            self.active = "gdi"
        return self.last


# ----------------------------------------------------------------- cursor drawing

ARROW = [(0, 0), (0, 17), (4, 13), (7, 20), (10, 19), (7, 12), (12, 12)]
_sprites = {}


def _cache_put(key, value):
    if len(_sprites) > 96:
        _sprites.clear()
    _sprites[key] = value
    return value


def add_glow(sprite, hotspot, color, invert_mask=None):
    """Soft halo in `color` behind any sprite, derived from its alpha."""
    pad = max(8, sprite.width // 3)
    if invert_mask is not None:
        padded = Image.new("L", (sprite.width + pad * 2, sprite.height + pad * 2), 0)
        padded.paste(invert_mask, (pad, pad))
        invert_mask = padded
    canvas = Image.new("RGBA", (sprite.width + pad * 2, sprite.height + pad * 2), (0, 0, 0, 0))
    alpha = Image.new("L", canvas.size, 0)
    alpha.paste(sprite.getchannel("A"), (pad, pad))
    alpha = alpha.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(pad / 2.5))
    alpha = alpha.point(lambda v: int(v * 0.55))
    halo = Image.new("RGBA", canvas.size, color + (0,))
    halo.putalpha(alpha)
    canvas.alpha_composite(halo)
    canvas.alpha_composite(sprite, (pad, pad))
    return canvas, (hotspot[0] + pad, hotspot[1] + pad), invert_mask


def arrow_sprite(color, k, glow):
    """Anti-aliased simple arrow, cached. Returns (image, hotspot)."""
    key = ("arrow", color, k, glow)
    if key in _sprites:
        return _sprites[key]
    ss, pad = 4, 3                             # supersample for smooth edges
    w, h = int(13 * k) + pad * 2, int(21 * k) + pad * 2
    big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    pts = [(pad * ss + px * k * ss, pad * ss + py * k * ss) for px, py in ARROW]
    outline = (255, 255, 255, 255) if luminance(color) < 80 else (0, 0, 0, 255)
    d.polygon(pts, fill=color + (255,))
    d.line(pts + [pts[0]], fill=outline, width=max(ss, int(1.4 * k * ss)), joint="curve")
    sprite, hot, mask = big.resize((w, h), Image.Resampling.LANCZOS), (pad, pad), None
    if glow:
        sprite, hot, mask = add_glow(sprite, hot, color)
    return _cache_put(key, (sprite, hot, mask))


def _render_cursor(hicon, w, h, background):
    """Draw a cursor onto a solid black (0) or white (255) background; returns HxWx3 uint8."""
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h      # top-down
    bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount = 1, 32
    bits = ctypes.c_void_p()
    dib = gdi32.CreateDIBSection(dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(dc, dib)
    try:
        ctypes.memset(bits, background, w * h * 4)
        user32.DrawIconEx(dc, 0, 0, hicon, w, h, 0, None, DI_NORMAL)
        raw = ctypes.string_at(bits, w * h * 4)
    finally:
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(dib)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)
    return np.frombuffer(raw, np.uint8).reshape(h, w, 4)[:, :, 2::-1].astype(np.int16)  # BGRA -> RGB


def real_cursor_sprite(hcursor, color, k, tint, glow):
    """The actual Windows cursor (arrow, I-beam, hand, resize...) as an RGBA sprite.
    Rendering it on black and on white recovers true transparency, including the
    'inverting' pixels old-style cursors like the I-beam use."""
    key = ("real", hcursor, color, k, tint, glow)
    if key in _sprites:
        return _sprites[key]
    hicon = user32.CopyIcon(hcursor)             # our own copy, in case the app frees its cursor
    if not hicon:
        return None
    try:
        info = ICONINFO()
        if not user32.GetIconInfo(hicon, ctypes.byref(info)):
            return None
        bm = BITMAP()
        gdi32.GetObjectW(info.hbmColor or info.hbmMask, ctypes.sizeof(BITMAP), ctypes.byref(bm))
        w = bm.bmWidth
        h = bm.bmHeight if info.hbmColor else bm.bmHeight // 2   # mono cursors stack AND/XOR masks
        hot = (info.xHotspot, info.yHotspot)
        for hbm in (info.hbmMask, info.hbmColor):
            if hbm:
                gdi32.DeleteObject(hbm)
        if w <= 0 or h <= 0:
            return None
        on_black = _render_cursor(hicon, w, h, 0)
        on_white = _render_cursor(hicon, w, h, 255)
    finally:
        user32.DestroyIcon(hicon)
    return _cache_put(key, build_cursor_sprite(on_black, on_white, hot, color, k, tint, glow))


def build_cursor_sprite(on_black, on_white, hot, color, k, tint, glow):
    """Pure-numpy half of real_cursor_sprite (kept separate so it can be tested anywhere)."""
    alpha = np.clip(255 - (on_white - on_black).mean(axis=2), 0, 255)
    invert = (on_black.mean(axis=2) - on_white.mean(axis=2)) > 96    # XOR / inverting pixels
    a = np.maximum(alpha, 1)[..., None] / 255.0
    rgb = np.clip(on_black / a, 0, 255)
    rgb[alpha == 0] = 0

    accent = np.array(color, dtype=float)
    mask = None
    if tint:
        lum = rgb @ np.array([0.299, 0.587, 0.114])
        visible = (alpha > 0) & ~invert
        body = visible & (lum > 150)
        rgb[body] = accent
        contrast = 255 if luminance(color) < 80 else 0
        if contrast:                              # dark colour: flip the dark outline to white
            rgb[visible & (lum <= 150) & (alpha > 160)] = 255
        if invert.any():                          # I-beam etc: solid colour + contrasting outline
            grown = np.asarray(Image.fromarray(invert.astype(np.uint8) * 255)
                               .filter(ImageFilter.MaxFilter(3))) > 0
            edge = grown & ~invert & (alpha < 128)
            rgb[edge], alpha[edge] = contrast, 255
            rgb[invert], alpha[invert] = accent, 255
    elif invert.any():                            # untinted: invert what's underneath, like Windows
        mask = Image.fromarray(invert.astype(np.uint8) * 255, "L")
        alpha[invert] = 0

    rgba = np.dstack([rgb, alpha]).astype(np.uint8)
    sprite = Image.fromarray(rgba, "RGBA")
    factor = k / 1.3                               # "Normal" draws the cursor at its true size
    if abs(factor - 1) > 0.01:
        new_size = (max(1, round(sprite.width * factor)), max(1, round(sprite.height * factor)))
        sprite = sprite.resize(new_size, Image.Resampling.LANCZOS)
        if mask is not None:
            mask = mask.resize(new_size, Image.Resampling.NEAREST)
        hot = (round(hot[0] * factor), round(hot[1] * factor))
    if glow:
        sprite, hot, mask = add_glow(sprite, hot, color, mask)
    return sprite, hot, mask


def draw_cursor(img, x, y, s, hcursor):
    color = CURSOR_COLORS[s["cursor_color"]][1]
    entry = None
    if s["cursor_style"] == "real" and hcursor:
        try:
            entry = real_cursor_sprite(hcursor, color, s["cursor_size"],
                                       s["cursor_tint"], s["cursor_glow"])
        except Exception:
            entry = None
    if entry is None:
        entry = arrow_sprite(color, s["cursor_size"], s["cursor_glow"])
    sprite, (hx, hy), invert_mask = entry
    pos = (int(x) - hx, int(y) - hy)
    if invert_mask is not None:                  # true inversion of the picture under the I-beam
        box = (pos[0], pos[1], pos[0] + sprite.width, pos[1] + sprite.height)
        img.paste(ImageOps.invert(img.crop(box)), pos, invert_mask)
    img.paste(sprite, pos, sprite)


def make_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((3, 6, 41, 34), radius=4, fill=(110, 110, 120, 255))
    d.rounded_rectangle((19, 20, 61, 49), radius=4, fill=(37, 99, 235, 255),
                        outline=(255, 255, 255, 255), width=3)
    d.rectangle((36, 49, 44, 55), fill=(255, 255, 255, 255))
    d.rectangle((29, 55, 51, 59), fill=(255, 255, 255, 255))
    return img


# ----------------------------------------------------------------- mirror window

class Mirror:
    def __init__(self, root, app):
        self.app = app
        self.visible = False
        self.captured = False
        self.photo = None
        self.job = None
        self.bar_state = None      # (state, border width) currently applied
        self.geom = None           # (offset_x, offset_y, scale) of the picture inside the window
        self.base = None           # scaled frame without the cursor
        self.base_layout = None
        self.drawn_key = None      # what's currently on screen, to skip identical redraws
        self._press = None
        self._dragging = False
        self.edge_job = None
        self._at_edge = False
        self._bumps = []

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)          # no title bar / borders
        self.win.attributes("-topmost", True)
        self.win.configure(bg="black")

        self.label = tk.Label(self.win, bg="black", bd=0)
        self.label.pack(fill="both", expand=True)

        # toolbar: title on the left, buttons on the right
        self.bar = tk.Frame(self.win, bg=BAR_BG, height=BAR_H)
        self.bar.pack_propagate(False)
        self.title = tk.Label(self.bar, bg=BAR_BG, fg=BAR_FG, font=("Segoe UI", 10),
                              anchor="w", padx=12)
        self.title.pack(side="left", fill="both", expand=True)
        self.close_btn = self._button("✕", self.hide, CLOSE_HOVER, font_size=12)
        self.mode_btn = self._button("", self._toggle_mode, BTN_HOVER)
        self.capture_btn = self._button("Capture cursor", self.capture, BTN_HOVER)

        for w in (self.label, self.bar, self.title):
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_motion)
        self.label.bind("<ButtonRelease-1>", lambda e: self._on_release(e, click_captures=True))
        self.bar.bind("<ButtonRelease-1>", lambda e: self._on_release(e, click_captures=False))
        self.title.bind("<ButtonRelease-1>", lambda e: self._on_release(e, click_captures=False))
        self.win.bind("<MouseWheel>", self._wheel)
        self.win.bind("<Escape>", lambda e: self.hide())

        self.win.withdraw()
        self.configure()

    def _button(self, text, command, hover, font_size=10):
        b = tk.Label(self.bar, text=text, bg=BAR_BG, fg=BAR_FG,
                     font=("Segoe UI", font_size), padx=14, cursor="hand2")
        b.bind("<Enter>", lambda e: b.configure(bg=hover))
        b.bind("<Leave>", lambda e: b.configure(bg=b._base_bg))
        b._base_bg = BAR_BG
        b.bind("<ButtonRelease-1>", lambda e: command())
        return b

    @property
    def src(self):
        return self.app.monitors[self.app.settings["source"] - 1]

    @property
    def dst(self):
        return self.app.monitors[self.app.settings["target"] - 1]

    @property
    def is_pip(self):
        return self.app.settings["mode"] == "pip"

    def configure(self):
        s, m, src = self.app.settings, self.dst, self.src
        if self.is_pip:
            w = int(m["width"] * s["pip_scale"])
            h = int(w * src["height"] / src["width"])
            pos = s.get("pip_pos")
            if pos and self.app.on_a_monitor(pos[0] + w // 2, pos[1] + BAR_H // 2):
                x, y = pos
            else:
                margin = 24
                corner = s["pip_corner"]
                x = m["left"] + (m["width"] - w - margin if "right" in corner else margin)
                y = m["top"] + (margin if "top" in corner else m["height"] - h - margin - 48)
            self.box = (x, y, w, h)
        else:
            self.box = (m["left"], m["top"], m["width"], m["height"])
        x, y, w, h = self.box
        self.win.geometry(f"{w}x{h}{x:+d}{y:+d}")
        cur = "fleur" if self.is_pip else "arrow"
        self.label.configure(cursor=cur)
        self.title.configure(cursor=cur)
        self.drawn_key = None
        self._refresh_chrome()

    # ---- border + toolbar appearance (normal vs. captured)

    def _refresh_chrome(self):
        s = self.app.settings
        border, bar_bg, bar_fg = accent_palette(CURSOR_COLORS[s["cursor_color"]][1])
        self.win.configure(bg=border if self.captured else "black")

        for btn in (self.close_btn, self.mode_btn, self.capture_btn):
            btn.pack_forget()
        if self.captured:
            bg, fg = bar_bg, bar_fg
            how = f"{CAPTURE_KEY}"
            if s["edge_release"]:
                left = s["edge_release"] - len(self._bumps)
                how += (f" or bump an edge {s['edge_release']}×" if left == s["edge_release"]
                        else f" or bump {left} more time{'s' if left != 1 else ''}")
            text = f"●  Cursor captured on Monitor {s['source']}   ·   {how} to release"
        else:
            bg, fg = BAR_BG, BAR_FG
            text = f"Monitor {s['source']}   ·   Esc to close"
            if self.app.capture_available:
                text += "   ·   click to capture cursor"
            self.close_btn.pack(side="right", fill="y")
            self.mode_btn.configure(text="Full screen" if self.is_pip else "Picture-in-picture")
            self.mode_btn.pack(side="right", fill="y")
            if self.app.capture_available:
                self.capture_btn.pack(side="right", fill="y")
        self.bar.configure(bg=bg)
        self.title.configure(bg=bg, fg=fg, text=text)
        for btn in (self.close_btn, self.mode_btn, self.capture_btn):
            btn._base_bg = bg
            btn.configure(bg=bg, fg=fg)
        self.drawn_key = None
        self._update_bar()

    # ---- show / hide

    def toggle(self):
        self.hide() if self.visible else self.show()

    def show(self):
        self.visible = True
        self.base = None
        self.drawn_key = None
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self._tick()
        if self.app.settings["capture_on_open"]:
            self.capture()

    def hide(self):
        was_visible, was_captured = self.visible, self.captured
        self.release()                  # if captured, this already centers the cursor
        if was_visible and not was_captured and self.app.settings["center_on_close"]:
            self._center_cursor()
        self.visible = False
        self._press, self._dragging = None, False
        if self.job:
            self.win.after_cancel(self.job)
            self.job = None
        self._set_bar("hidden")
        self.win.withdraw()

    def _toggle_mode(self):
        self.app.update_setting("mode", "fullscreen" if self.is_pip else "pip")

    # ---- cursor capture

    def toggle_capture(self):
        self.release() if self.captured else self.capture()

    def capture(self, at=None):
        if self.captured or not self.app.capture_available:
            return
        if not self.visible:
            self.show()
            if self.captured:          # capture_on_open already did it
                return
        src = self.src
        tx, ty = src["left"] + src["width"] // 2, src["top"] + src["height"] // 2
        if at is None:
            at = cursor_position()
        mapped = self._mirror_to_source(*at) if at else None
        if mapped:
            tx, ty = mapped
        self.captured = True
        self._refresh_chrome()
        self._tick_now()                # redraw with the border before moving the cursor
        user32.SetCursorPos(tx, ty)
        clip_cursor(src)
        self._at_edge, self._bumps = False, []
        self._edge_watch()

    def release(self):
        if not self.captured:
            return
        self.captured = False
        clip_cursor(None)
        if self.edge_job:
            self.win.after_cancel(self.edge_job)
            self.edge_job = None
        self._bumps = []
        self._center_cursor()           # always come back to the middle of the main screen
        self._refresh_chrome()

    def _center_cursor(self):
        """Put the cursor in the middle of the monitor the mirror is shown on."""
        dst = self.dst
        user32.SetCursorPos(dst["left"] + dst["width"] // 2, dst["top"] + dst["height"] // 2)

    def _edge_watch(self):
        """While captured, count bumps against the monitor's edges; enough quick bumps
        releases the cursor (the clip stops it from actually leaving)."""
        self.edge_job = None
        if not self.captured:
            return
        need = self.app.settings["edge_release"]
        pos = cursor_position() if need else None
        if pos:
            src = self.src
            x, y = pos[0] - src["left"], pos[1] - src["top"]
            dist = min(x, src["width"] - 1 - x, y, src["height"] - 1 - y)
            now = time.monotonic()
            expired = [t for t in self._bumps if now - t >= EDGE_BUMP_WINDOW]
            if dist <= 0 and not self._at_edge:
                self._at_edge = True
                self._bumps = [t for t in self._bumps if now - t < EDGE_BUMP_WINDOW] + [now]
                if len(self._bumps) >= need:
                    self.release()
                    return
                self._refresh_chrome()          # "bump N more times"
            elif dist >= EDGE_REARM_PX:
                self._at_edge = False
            if expired and self._bumps:
                self._bumps = [t for t in self._bumps if now - t < EDGE_BUMP_WINDOW]
                self._refresh_chrome()
        self.edge_job = self.win.after(15, self._edge_watch)

    def _mirror_to_source(self, px, py):
        """Mirror-window screen point -> point on the source monitor (None if outside the picture)."""
        if not self.geom:
            return None
        src = self.src
        ox, oy, scale = self.geom
        sx = (px - self.box[0] - ox) / scale
        sy = (py - self.box[1] - oy) / scale
        if 0 <= sx < src["width"] and 0 <= sy < src["height"]:
            return src["left"] + int(sx), src["top"] + int(sy)
        return None

    # ---- toolbar

    def _set_bar(self, state):
        """The toolbar always sits *above* the picture, never on top of it, so every
        part of the mirrored screen (title bars, close buttons) stays visible and clickable.
        hidden: no toolbar (mirror closed). docked: toolbar strip + optional border."""
        b = BORDER_W if self.captured else 0
        key = (state, b)
        if key == self.bar_state:
            return
        self.bar.pack_forget()
        if state == "docked":
            self.bar.pack(side="top", fill="x", padx=b, pady=(b, 0), before=self.label)
            self.label.pack_configure(padx=b, pady=(0, b))
        else:
            self.label.pack_configure(padx=0, pady=0)
        self.bar_state = key

    def _update_bar(self):
        if self.visible:
            self._set_bar("docked")

    # ---- click to capture, drag to move, scroll to resize

    def _on_press(self, e):
        self._press = (e.x_root, e.y_root, e.x_root - self.box[0], e.y_root - self.box[1])
        self._dragging = False

    def _on_motion(self, e):
        if not self._press or not self.is_pip:
            return
        sx, sy, dx, dy = self._press
        if not self._dragging and abs(e.x_root - sx) + abs(e.y_root - sy) > 5:
            self._dragging = True
        if self._dragging:
            x, y = e.x_root - dx, e.y_root - dy
            _, _, w, h = self.box
            self.box = (x, y, w, h)
            self.win.geometry(f"{x:+d}{y:+d}")

    def _on_release(self, e, click_captures):
        was_drag = self._dragging
        self._press, self._dragging = None, False
        if was_drag:
            self.app.set_pip_pos(self.box[0], self.box[1])
        elif click_captures:
            self.capture(at=(e.x_root, e.y_root))

    def _wheel(self, e):
        if not self.is_pip:
            return
        step = 0.05 if e.delta > 0 else -0.05
        new = min(0.90, max(0.10, round(self.app.settings["pip_scale"] + step, 2)))
        self.app.update_setting("pip_scale", new)

    # ---- frame loop

    def _tick_now(self):
        if self.job:
            self.win.after_cancel(self.job)
            self.job = None
        self._tick()

    def _layout(self):
        """Where the picture goes: below the toolbar, inside the border when captured."""
        b = BORDER_W if self.captured else 0
        top = b + BAR_H
        _, _, w, h = self.box
        return b, top, w - 2 * b, h - top - b

    def _tick(self):
        if not self.visible:
            return
        start = time.perf_counter()
        s, src = self.app.settings, self.src
        if self.captured:
            clip_cursor(src)            # Windows can drop the clip (focus changes etc.); re-assert it

        # 1. screen: only re-scale when it changed (GPU capture tells us) or the layout did
        layout = self._layout()
        need = self.base is None or self.base_layout != layout
        frame = self.app.capturer.grab(src, need_frame=need)
        if frame is not None:
            b, top, iw, ih = layout
            fh, fw = frame.shape[:2]
            scale = min(iw / fw, ih / fh)
            size = (max(1, int(fw * scale)), max(1, int(fh * scale)))
            if size != (fw, fh):
                interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR   # INTER_AREA keeps text crisp
                frame = cv2.resize(frame, size, interpolation=interp)
            self.base = Image.fromarray(frame)
            self.base_layout = layout
            self.geom = (b + (iw - size[0]) // 2, top + (ih - size[1]) // 2, scale)
            self.drawn_key = None

        # 2. cursor: only redraw when it moved, changed shape, or a setting changed
        cursor = None
        if s["show_cursor"] and self.geom:
            info = cursor_info()
            if info:
                cx, cy = info[0] - src["left"], info[1] - src["top"]
                if 0 <= cx < src["width"] and 0 <= cy < src["height"]:
                    scale = self.geom[2]
                    cursor = (int(cx * scale), int(cy * scale), info[2])
        key = (cursor, s["cursor_style"], s["cursor_color"], s["cursor_size"],
               s["cursor_tint"], s["cursor_glow"])

        # 3. push to the window only if something visible changed
        if self.base is not None and key != self.drawn_key:
            img = self.base
            if cursor:
                img = self.base.copy()
                draw_cursor(img, cursor[0], cursor[1], s, cursor[2])
            if self.photo is not None and (self.photo.width(), self.photo.height()) == img.size:
                self.photo.paste(img)               # reuse the existing Tk image buffer
            else:
                self.photo = ImageTk.PhotoImage(img)
                self.label.configure(image=self.photo)
            self.drawn_key = key

        self._update_bar()
        elapsed_ms = (time.perf_counter() - start) * 1000
        self.job = self.win.after(max(1, int(1000 / s["fps"] - elapsed_ms)), self._tick)


# ----------------------------------------------------------------- app + tray

class App:
    def __init__(self):
        with mss.mss() as sct:
            self.monitors = sct.monitors[1:]       # [0] is the whole virtual desktop
        self.settings = load_settings()
        self._fix_monitor_choices()
        self.capture_available = True
        self.capturer = Capturer(self)

        self.events = queue.Queue()
        self.root = tk.Tk()
        self.root.title(INSTANCE_WINDOW_TITLE)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)   # a newer launch asks us to quit
        self.root.withdraw()
        self.mirror = Mirror(self.root, self)
        self.icon = pystray.Icon("monitor_mirror", make_tray_image(),
                                 f"{APP_NAME} {APP_VERSION} ({TOGGLE_KEY})", self._build_menu())

    def _fix_monitor_choices(self):
        n = len(self.monitors)
        s = self.settings
        if not 1 <= s["source"] <= n:
            s["source"] = n
        if not 1 <= s["target"] <= n:
            s["target"] = 1
        if s["source"] == s["target"] and n > 1:
            s["target"] = 1 if s["source"] != 1 else 2

    def on_a_monitor(self, x, y):
        return any(m["left"] <= x < m["left"] + m["width"] and
                   m["top"] <= y < m["top"] + m["height"] for m in self.monitors)

    # all UI changes happen on the Tk thread; tray/hotkey threads post here
    def post(self, fn):
        self.events.put(("call", fn))

    def update_setting(self, key, value):
        s = self.settings
        if key in ("source", "target", "mode"):
            self.mirror.release()
        if key in ("source", "target"):
            other = "target" if key == "source" else "source"
            if s[other] == value:              # picking the same monitor swaps them
                s[other] = s[key]
        if key in ("pip_corner", "target"):
            s["pip_pos"] = None                # back to a preset position
        s[key] = value
        if key in ("source", "target", "engine"):
            self.capturer.reset()
            self.mirror.base = None
        save_settings(s)
        self.mirror.configure()
        self.icon.update_menu()

    def set_pip_pos(self, x, y):
        self.settings["pip_pos"] = [x, y]
        save_settings(self.settings)
        self.icon.update_menu()

    def toggle_startup(self):
        try:
            set_startup(not startup_enabled())
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't change startup setting:\n{e}")
        self.icon.update_menu()

    def quit(self):
        self.mirror.hide()
        self.capturer.reset()
        self.icon.stop()
        self.root.quit()

    def _engine_status(self, item):
        if not HAVE_DXCAM:
            return "Running on: GDI  (install dxcam for GPU capture)"
        if self.settings["engine"] == "gpu" and self.capturer.active != "gpu" and self.mirror.visible:
            return "Running on: GDI  (GPU capture unavailable right now)"
        return f"Running on: {'GPU' if self.settings['engine'] == 'gpu' else 'GDI'}"

    def _build_menu(self):
        Item, Menu = pystray.MenuItem, pystray.Menu
        s = self.settings

        def radio(label, key, value, enabled=True, checked=None):
            return Item(label,
                        lambda icon, item: self.post(lambda: self.update_setting(key, value)),
                        checked=checked or (lambda item: s[key] == value), radio=True,
                        enabled=enabled)

        def toggle(label, key, enabled=True):
            return Item(label,
                        lambda icon, item: self.post(lambda: self.update_setting(key, not s[key])),
                        checked=lambda item: bool(s[key]), enabled=enabled)

        def note(text):
            return Item(text, lambda icon, item: None, enabled=False)

        def monitors_menu(key):
            return Menu(*[radio(f"Monitor {i}   ({m['width']}×{m['height']})", key, i)
                          for i, m in enumerate(self.monitors, 1)])

        def corner(label, value):
            return radio(label, "pip_corner", value,
                         checked=lambda item: s["pip_corner"] == value and not s["pip_pos"])

        pip_only = lambda item: s["mode"] == "pip"
        cursor_on = lambda item: s["show_cursor"]
        real_on = lambda item: s["show_cursor"] and s["cursor_style"] == "real"
        can_capture = lambda item: self.capture_available

        return Menu(
            note(f"{APP_NAME}  v{APP_VERSION}"),
            Menu.SEPARATOR,
            Item(f"Show / hide   ({TOGGLE_KEY})",
                 lambda icon, item: self.post(self.mirror.toggle), default=True),
            Item(f"Capture cursor   ({CAPTURE_KEY})",
                 lambda icon, item: self.post(self.mirror.capture), enabled=can_capture),
            Menu.SEPARATOR,
            Item("Mirror monitor", monitors_menu("source")),
            Item("Show it on", monitors_menu("target")),
            Item("Display", Menu(
                radio("Full screen", "mode", "fullscreen"),
                radio("Picture-in-picture", "mode", "pip"),
            )),
            Item("Picture-in-picture size", Menu(
                *[radio(f"{round(v * 100)}%", "pip_scale", v) for v in SCALES],
                Menu.SEPARATOR,
                note("Tip: scroll over the window to resize"),
            ), enabled=pip_only),
            Item("Picture-in-picture position", Menu(
                corner("Top left", "top-left"),
                corner("Top right", "top-right"),
                corner("Bottom left", "bottom-left"),
                corner("Bottom right", "bottom-right"),
                Item("Custom (dragged)", lambda icon, item: None,
                     checked=lambda item: bool(s["pip_pos"]), radio=True, enabled=False),
                Menu.SEPARATOR,
                note("Tip: drag the window to move it"),
            ), enabled=pip_only),
            Item("Performance", Menu(
                *[radio(f"{f} fps", "fps", f) for f in FPS_OPTIONS],
                Menu.SEPARATOR,
                radio("GPU capture (recommended)", "engine", "gpu",
                      enabled=lambda item: HAVE_DXCAM),
                radio("Compatibility capture (GDI)", "engine", "gdi"),
                Item(self._engine_status, lambda icon, item: None, enabled=False),
            )),
            Item("Color  (cursor + capture border)", Menu(
                *[radio(name, "cursor_color", key) for key, (name, _) in CURSOR_COLORS.items()],
            )),
            Item("Mouse cursor", Menu(
                toggle("Show cursor on mirror", "show_cursor"),
                Menu.SEPARATOR,
                radio("Real cursor shape", "cursor_style", "real", enabled=cursor_on),
                radio("Simple arrow", "cursor_style", "arrow", enabled=cursor_on),
                toggle("Tint real cursor with color", "cursor_tint", enabled=real_on),
                Menu.SEPARATOR,
                *[radio(name, "cursor_size", k, enabled=cursor_on) for name, k in CURSOR_SIZES],
                Menu.SEPARATOR,
                toggle("Soft glow", "cursor_glow", enabled=cursor_on),
            )),
            Item("Cursor capture", Menu(
                toggle("Capture automatically when the mirror opens", "capture_on_open",
                       enabled=can_capture),
                Menu.SEPARATOR,
                note("Release by bumping a screen edge:"),
                radio("Off", "edge_release", 0, enabled=can_capture),
                *[radio(f"{n} quick bumps", "edge_release", n, enabled=can_capture) for n in (2, 3, 4)],
                Menu.SEPARATOR,
                note(f"Or release with {CAPTURE_KEY}, or close the mirror"),
                Menu.SEPARATOR,
                toggle("Center the cursor when closing the mirror", "center_on_close"),
            )),
            Menu.SEPARATOR,
            Item("Start with Windows",
                 lambda icon, item: self.post(self.toggle_startup),
                 checked=lambda item: startup_enabled()),
            Item("Quit", lambda icon, item: self.post(self.quit)),
        )

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "call":
                    payload()
                elif kind == "hotkey-failed":
                    self.mirror._refresh_chrome()
                    self.icon.update_menu()
                    extra = ("\nCursor capture is turned off, since it needs its release hotkey."
                             if CAPTURE_KEY in payload else "")
                    messagebox.showwarning(
                        APP_NAME,
                        f"Couldn't register {' and '.join(payload)} - another app may be using "
                        f"{'them' if len(payload) > 1 else 'it'}.{extra}\n"
                        "You can still open the mirror from the tray icon.")
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    def run(self):
        threading.Thread(target=hotkey_listener, args=(self,), daemon=True).start()
        threading.Thread(target=self.icon.run, daemon=True).start()
        self._poll()
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--list", action="store_true", help="list monitors and exit")
    args = parser.parse_args()

    make_dpi_aware()

    if args.list:
        with mss.mss() as sct:
            for i, m in enumerate(sct.monitors[1:], 1):
                gpu = dxgi_output_for(m) if HAVE_DXCAM else None
                print(f"{i}: {m['width']}x{m['height']} at ({m['left']}, {m['top']})"
                      f"   GPU capture: {'yes' if gpu else 'no'}")
        return

    if not acquire_single_instance():
        user32.MessageBoxW(
            None,
            "An older version of Monitor Mirror is still running.\n\n"
            "Right-click its tray icon, choose Quit, then open this file again.\n"
            "(From now on, opening a newer version replaces the running one automatically.)",
            APP_NAME, 0x40)
        return

    atexit.register(lambda: clip_cursor(None))   # never leave the cursor trapped
    App().run()


if __name__ == "__main__":
    main()
