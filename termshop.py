#!/usr/bin/env python3
"""termshop -- a photo editor that lives in your terminal.

Usage: python termshop.py IMAGE

Preview is rendered with unicode half-blocks (2 pixels per cell), so it
works in any truecolor terminal. Edits are kept as an operation pipeline
and only applied to the full-resolution image on save.
"""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import time
from functools import reduce
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps
from rich.color import Color
from rich.markup import escape
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Footer, Input, OptionList, Static

PREVIEW_MAX = 1600  # longest side of the working preview
T = Image.Transpose

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def list_images(dirpath):
    """Images in a directory, sorted by name, hidden files excluded."""
    return sorted(
        p.resolve() for p in Path(dirpath).iterdir()
        if p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")
    )


# --------------------------------------------------------------------------
# Sidecar persistence: one `.termshop.json` per directory maps filename ->
# op pipeline, so unsaved edits survive restarts.
# --------------------------------------------------------------------------

SIDECAR_NAME = ".termshop.json"

KNOWN_OPS = {
    "rotate_cw", "rotate_ccw", "flip_h", "flip_v", "crop", "grayscale",
    "autocontrast", "invert", "blur", "sharpen", "gamma", "shadows",
    "highlights", "temp", "tint", "brightness", "contrast", "saturation",
    "sharpness", "curves", "angle",
}


def _clean_curves(arg):
    if not isinstance(arg, dict):
        return None
    out = {}
    for ch, pts in arg.items():
        if ch in ("rgb", "r", "g", "b") and isinstance(pts, list) and len(pts) >= 2 and all(
            isinstance(p, (list, tuple)) and len(p) == 2
            and all(isinstance(v, (int, float)) for v in p) for p in pts
        ):
            out[ch] = [[min(255, max(0, x)), min(255, max(0, y))] for x, y in pts]
    return out or None


def clean_ops(items):
    """Validate a raw (deserialized) op list into (name, arg) tuples."""
    out = []
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] in KNOWN_OPS:
            arg = tuple(item[1]) if isinstance(item[1], list) else item[1]
            if item[0] == "curves":
                arg = _clean_curves(arg)
                if arg is None:
                    continue
            out.append((item[0], arg))
    return out


def load_sidecar(sidecar):
    """Read {filename: ops} from a sidecar file; tolerant of junk."""
    try:
        data = json.loads(Path(sidecar).read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for name, ops in data.get("edits", {}).items():
        clean = clean_ops(ops) if isinstance(ops, list) else []
        if clean:
            out[name] = clean
    return out


def write_sidecar(sidecar, edits):
    """Write {filename: ops}; an empty mapping removes the file."""
    sidecar = Path(sidecar)
    try:
        if edits:
            sidecar.write_text(json.dumps({"version": 1, "edits": edits}, indent=1))
        elif sidecar.exists():
            sidecar.unlink()
    except OSError:
        pass  # read-only dir etc. -- edits just stay in memory


# --------------------------------------------------------------------------
# Clipboard (best effort, no hard dependencies)
# --------------------------------------------------------------------------

def clipboard_copy_png(path):
    """Copy a PNG file to the system clipboard. Returns True on success."""
    if sys.platform == "darwin":
        script = f'set the clipboard to (read (POSIX file "{path}") as «class PNGf»)'
        return subprocess.run(["osascript", "-e", script],
                              capture_output=True).returncode == 0
    for cmd in (["wl-copy", "-t", "image/png"],
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"]):
        if shutil.which(cmd[0]):
            with open(path, "rb") as f:
                return subprocess.run(cmd, stdin=f, capture_output=True).returncode == 0
    return False


def clipboard_paste_png():
    """Clipboard image as PNG bytes: returns (data, None) or (None, reason)."""
    if sys.platform == "darwin":
        if not shutil.which("pngpaste"):
            return None, "needs pngpaste (brew install pngpaste)"
        r = subprocess.run(["pngpaste", "-"], capture_output=True)
        return (r.stdout, None) if r.returncode == 0 and r.stdout else (None, "no image on clipboard")
    for cmd in (["wl-paste", "-t", "image/png"],
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]):
        if shutil.which(cmd[0]):
            r = subprocess.run(cmd, capture_output=True)
            return (r.stdout, None) if r.returncode == 0 and r.stdout else (None, "no image on clipboard")
    return None, "no clipboard tool found (wl-clipboard or xclip)"


SPARK = " ▁▂▃▄▅▆▇█"


def spark_hist(hist256, width=30):
    """Compress a 256-bin channel histogram into a unicode sparkline."""
    per = 256 / width
    edges = [int(i * per) for i in range(width + 1)]
    # average per bin, not sum: bucket widths alternate 8/9 bins
    buckets = [sum(hist256[a:b]) / (b - a) for a, b in zip(edges, edges[1:])]
    peak = max(buckets) or 1
    return "".join(SPARK[round(8 * b / peak)] for b in buckets)

ENHANCERS = {
    "brightness": ImageEnhance.Brightness,
    "contrast": ImageEnhance.Contrast,
    "saturation": ImageEnhance.Color,
    "sharpness": ImageEnhance.Sharpness,
}


# --------------------------------------------------------------------------
# Operation pipeline: every edit is a (name, arg) tuple. Undo/redo just
# moves ops between two stacks; save replays the pipeline at full res.
# --------------------------------------------------------------------------

def _lut(fn):
    """Build an 8-bit lookup table from a float function, clamped to [0, 255]."""
    return [min(255, max(0, round(fn(v)))) for v in range(256)]


_IDENT = list(range(256))

CURVE_IDENTITY = [(0, 0), (255, 255)]


def _max_rect(w, h, deg):
    """Largest axis-aligned rectangle inside a w x h image rotated by deg
    (the classic straighten auto-crop)."""
    a = math.radians(abs(deg) % 180)
    if a > math.pi / 2:
        a = math.pi - a
    sin_a, cos_a = math.sin(a), math.cos(a)
    if w <= 0 or h <= 0 or sin_a == 0:
        return w, h
    longer = w >= h
    side_long, side_short = (w, h) if longer else (h, w)
    if side_short <= 2 * sin_a * cos_a * side_long or abs(sin_a - cos_a) < 1e-10:
        x = 0.5 * side_short
        wr, hr = (x / sin_a, x / cos_a) if longer else (x / cos_a, x / sin_a)
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        wr = (w * cos_a - h * sin_a) / cos_2a
        hr = (h * cos_a - w * sin_a) / cos_2a
    return int(wr + 1e-6), int(hr + 1e-6)


def monotone_lut(points):
    """256-entry LUT through control points via Fritsch-Carlson monotone
    cubic interpolation -- smooth, and never overshoots between points."""
    pts = sorted((float(x), float(y)) for x, y in points)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    n = len(pts)
    if n < 2:
        return [min(255, max(0, round(ys[0] if pts else 0)))] * 256
    d = [(ys[i + 1] - ys[i]) / max(1e-6, xs[i + 1] - xs[i]) for i in range(n - 1)]
    m = [d[0]] + [(d[i - 1] + d[i]) / 2 if d[i - 1] * d[i] > 0 else 0.0
                  for i in range(1, n - 1)] + [d[-1]]
    for i in range(n - 1):
        if d[i] == 0:
            m[i] = m[i + 1] = 0.0
        else:
            a, b = m[i] / d[i], m[i + 1] / d[i]
            s = a * a + b * b
            if s > 9:
                t = 3 / s ** 0.5
                m[i] = t * a * d[i]
                m[i + 1] = t * b * d[i]
    lut, j = [], 0
    for v in range(256):
        x = float(v)
        if x <= xs[0]:
            y = ys[0]
        elif x >= xs[-1]:
            y = ys[-1]
        else:
            while xs[j + 1] < x:
                j += 1
            h = xs[j + 1] - xs[j]
            t = (x - xs[j]) / h
            y = ((1 + 2 * t) * (1 - t) ** 2 * ys[j] + t * (1 - t) ** 2 * h * m[j]
                 + t * t * (3 - 2 * t) * ys[j + 1] + t * t * (t - 1) * h * m[j + 1])
        lut.append(min(255, max(0, round(y))))
    return lut


def apply_op(img, op):
    name, arg = op
    if name == "rotate_cw":
        return img.transpose(T.ROTATE_270)
    if name == "rotate_ccw":
        return img.transpose(T.ROTATE_90)
    if name == "flip_h":
        return img.transpose(T.FLIP_LEFT_RIGHT)
    if name == "flip_v":
        return img.transpose(T.FLIP_TOP_BOTTOM)
    if name == "crop":
        w, h = img.size
        l, t, r, b = arg
        return img.crop((round(l * w), round(t * h), round(r * w), round(b * h)))
    if name in ENHANCERS:
        return ENHANCERS[name](img).enhance(arg)
    if name == "grayscale":
        return img.convert("L").convert("RGB")
    if name == "autocontrast":
        return ImageOps.autocontrast(img)
    if name == "invert":
        return ImageOps.invert(img)
    if name == "blur":
        return img.filter(ImageFilter.GaussianBlur(arg))
    if name == "sharpen":
        return img.filter(ImageFilter.UnsharpMask(radius=2, percent=int(arg)))
    if name == "gamma":
        inv = 1.0 / arg  # arg > 1 brightens midtones
        lut = _lut(lambda v: 255 * (v / 255) ** inv)
        return img.point(lut * 3)
    if name == "shadows":
        # lift (+) or deepen (-) the dark end, leaving highlights alone
        lut = _lut(lambda v: v + arg * 255 * (1 - v / 255) ** 2)
        return img.point(lut * 3)
    if name == "highlights":
        # raise (+) or recover (-) the bright end, leaving shadows alone
        lut = _lut(lambda v: v + arg * 255 * (v / 255) ** 2)
        return img.point(lut * 3)
    if name == "temp":
        # white balance: + warms (red up, blue down), - cools
        r = _lut(lambda v: v * (1 + arg))
        b = _lut(lambda v: v * (1 - arg))
        return img.point(r + _IDENT + b)
    if name == "tint":
        # + toward magenta (red/blue up, green down), - toward green
        g = _lut(lambda v: v * (1 - arg))
        rb = _lut(lambda v: v * (1 + arg / 2))
        return img.point(rb + g + rb)
    if name == "angle":
        # straighten: arg degrees clockwise; rotate then auto-crop black wedges
        wr, hr = _max_rect(*img.size, arg)
        out = img.rotate(-arg, resample=Image.Resampling.BICUBIC, expand=True)
        x0 = round(out.width / 2 - wr / 2)
        y0 = round(out.height / 2 - hr / 2)
        return out.crop((x0, y0, x0 + wr, y0 + hr))
    if name == "curves":
        # arg: {channel: [[x, y], ...]} with channel in rgb/r/g/b
        out = img
        for ch, pts in arg.items():
            lut = monotone_lut(pts)
            if ch == "rgb":
                out = out.point(lut * 3)
            else:
                bands = [_IDENT] * 3
                bands["rgb".index(ch)] = lut
                out = out.point(bands[0] + bands[1] + bands[2])
        return out
    raise ValueError(f"unknown op {name!r}")


def op_size(size, op):
    """Track full-resolution dimensions through the pipeline without rendering."""
    w, h = size
    name, arg = op
    if name in ("rotate_cw", "rotate_ccw"):
        return (h, w)
    if name == "angle":
        return _max_rect(w, h, arg)
    if name == "crop":
        l, t, r, b = arg
        return (round(r * w) - round(l * w), round(b * h) - round(t * h))
    return (w, h)


def op_label(op):
    name, arg = op
    if name in ENHANCERS:
        return f"{name} x{arg:g}"
    if name == "crop":
        return "crop {:.0f}%x{:.0f}%".format((arg[2] - arg[0]) * 100, (arg[3] - arg[1]) * 100)
    if name == "blur":
        return f"blur r={arg:g}"
    if name == "gamma":
        return f"gamma x{arg:g}"
    if name in ("shadows", "highlights", "temp", "tint"):
        return f"{name} {arg:+.2f}"
    if name == "curves":
        return "curves " + "+".join(arg)
    if name == "angle":
        return f"straighten {arg:+.1f}\N{DEGREE SIGN}"
    return name.replace("_", " ")


# --------------------------------------------------------------------------
# Image pane: renders the processed preview with half-blocks and handles
# mouse-drag crop selection.
# --------------------------------------------------------------------------

ASPECTS = [(None, "free"), (1.0, "1:1"), (4 / 3, "4:3"), (3 / 2, "3:2"), (16 / 9, "16:9")]


class ImageView(Widget, can_focus=True):

    DEFAULT_CSS = "ImageView { background: #101010; }"

    def __init__(self):
        super().__init__()
        self._img = None          # processed preview (PIL)
        self._grid = b""          # RGB bytes of the letterboxed canvas
        self._base = None         # letterboxed canvas as PIL image (pre-overlay)
        self._gw = self._gh = 0   # canvas size in half-block pixels (cols, rows*2)
        self._dw = self._dh = 0   # displayed image size within the canvas
        self._ox = self._oy = 0   # letterbox offsets
        self._sel = None          # crop selection in canvas pixel coords
        self._drag = None
        self.crop_mode = False
        self._aspect_idx = 0      # index into ASPECTS; locks crop ratio when set
        self._zoom = 1.0          # 1.0 = fit to pane
        self._cx = self._cy = 0.5 # view center as image fractions
        self._box = None          # visible source region (sx, sy, vw, vh) in image px
        self._pan_drag = None

    def set_image(self, img):
        self._img = img
        self._sel = None
        self._rebuild()

    def on_resize(self):
        self._rebuild()

    def _units(self):
        """Canvas pixels per terminal cell as (x, y). Half-blocks: 1x2."""
        return (1, 2)

    def _rebuild(self):
        cols, rows = self.size.width, self.size.height
        if not cols or not rows or self._img is None:
            self._grid = b""
            self._base = None
            self.refresh()
            return
        ux, uy = self._units()
        gw, gh = cols * ux, rows * uy
        w, h = self._img.size
        scale = min(gw / w, gh / h) * self._zoom
        vw = min(w, gw / scale)   # visible source window, image px
        vh = min(h, gh / scale)
        # clamp the view center so the window stays inside the image
        self._cx = 0.5 if vw >= w else min(max(self._cx, vw / (2 * w)), 1 - vw / (2 * w))
        self._cy = 0.5 if vh >= h else min(max(self._cy, vh / (2 * h)), 1 - vh / (2 * h))
        sx = self._cx * w - vw / 2
        sy = self._cy * h - vh / 2
        dw = max(1, min(gw, round(vw * scale)))
        dh = max(1, min(gh, round(vh * scale)))
        canvas = Image.new("RGB", (gw, gh), (16, 16, 16))
        ox, oy = (gw - dw) // 2, (gh - dh) // 2
        canvas.paste(
            self._img.resize((dw, dh), Image.Resampling.LANCZOS,
                             box=(sx, sy, sx + vw, sy + vh)),
            (ox, oy),
        )
        self._base = canvas
        self._gw, self._gh = gw, gh
        self._dw, self._dh = dw, dh
        self._ox, self._oy = ox, oy
        self._box = (sx, sy, vw, vh)
        self._compose_canvas()
        if hasattr(self.app, "_update_side"):
            self.app._update_side()

    def _compose_canvas(self):
        """Bake the crop-selection overlay (dim outside) and hand off to the renderer."""
        if self._base is None:
            return
        canvas = self._base
        if self._sel is not None:
            x0, y0, x1, y1 = self._norm_sel()
            canvas = canvas.point(lambda v: v // 3)
            canvas.paste(self._base.crop((x0, y0, x1 + 1, y1 + 1)), (x0, y0))
        self._present(canvas)

    def _norm_sel(self):
        x0, y0, x1, y1 = self._sel
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        return (max(0, x0), max(0, y0), min(self._gw - 1, x1), min(self._gh - 1, y1))

    def _present(self, canvas):
        """Half-block backend: store raw RGB bytes for render_line."""
        self._grid = canvas.tobytes()
        self.refresh()

    # -- zoom / pan ------------------------------------------------------------

    def zoom_by(self, factor):
        new = min(32.0, max(1.0, self._zoom * factor))
        if new != self._zoom:
            self._zoom = new
            self._rebuild()

    def reset_view(self):
        self._zoom, self._cx, self._cy = 1.0, 0.5, 0.5
        self._rebuild()

    def pan_by(self, dx, dy):
        """Pan by fractions of the visible window (dx, dy in [-1, 1])."""
        if self._box is None or self._img is None or self._zoom <= 1.0:
            return
        sx, sy, vw, vh = self._box
        w, h = self._img.size
        self._cx += dx * vw / w
        self._cy += dy * vh / h
        self._rebuild()

    def _px(self, x, y):
        i = (y * self._gw + x) * 3
        return self._grid[i], self._grid[i + 1], self._grid[i + 2]

    def render_line(self, y):
        if not self._grid or y * 2 + 1 >= self._gh or self._gw != self.size.width:
            return Strip.blank(self.size.width)
        segs = []
        for x in range(self._gw):
            tr, tg, tb = self._px(x, y * 2)
            br, bg, bb = self._px(x, y * 2 + 1)
            segs.append(
                Segment("▀", Style(color=Color.from_rgb(tr, tg, tb),
                                        bgcolor=Color.from_rgb(br, bg, bb)))
            )
        return Strip(segs, self._gw)

    # -- mouse: crop selection, or pan when zoomed -----------------------------

    def _local_cell(self, ev):
        """Event position in this widget's cell coordinates (child-safe)."""
        r = self.region
        return ev.screen_x - r.x, ev.screen_y - r.y

    def on_mouse_down(self, ev: events.MouseDown):
        cx, cy = self._local_cell(ev)
        ux, uy = self._units()
        if self.crop_mode:
            self.capture_mouse()
            self._drag = (cx * ux, cy * uy)
            self._sel = (cx * ux, cy * uy, cx * ux + ux - 1, cy * uy + uy - 1)
            self._compose_canvas()
        elif self._zoom > 1.0:
            self.capture_mouse()
            self._pan_drag = (cx, cy)

    def cycle_aspect(self):
        self._aspect_idx = (self._aspect_idx + 1) % len(ASPECTS)
        return ASPECTS[self._aspect_idx][1]

    @property
    def aspect(self):
        return ASPECTS[self._aspect_idx][0]

    def _constrain(self, x0, y0, ex, ey):
        """Snap the moving corner to the locked aspect ratio (canvas px are
        square-ish, and display scale is uniform, so this is exact in image px)."""
        ratio = self.aspect
        if ratio is None:
            return ex, ey
        dx, dy = ex - x0, ey - y0
        w, h = abs(dx), abs(dy)
        if w < h * ratio:
            w = h * ratio
        else:
            h = w / ratio
        return (round(x0 + (w if dx >= 0 else -w)),
                round(y0 + (h if dy >= 0 else -h)))

    def on_mouse_move(self, ev: events.MouseMove):
        cx, cy = self._local_cell(ev)
        ux, uy = self._units()
        if self._drag is not None:
            x0, y0 = self._drag
            ex, ey = self._constrain(x0, y0, cx * ux + ux - 1, cy * uy + uy - 1)
            self._sel = (x0, y0, ex, ey)
            self._compose_canvas()
        elif self._pan_drag is not None:
            px, py = self._pan_drag
            dxc, dyc = cx - px, cy - py
            if (dxc or dyc) and self._box is not None:
                sx, sy, vw, vh = self._box
                w, h = self._img.size
                self._cx -= dxc * ux * (vw / self._dw) / w
                self._cy -= dyc * uy * (vh / self._dh) / h
                self._pan_drag = (cx, cy)
                self._rebuild()

    def on_mouse_up(self, ev: events.MouseUp):
        if self._drag is None and self._pan_drag is None:
            return
        self.release_mouse()
        self._drag = None
        self._pan_drag = None

    def on_mouse_scroll_up(self, ev):
        self.zoom_by(1.25)
        ev.stop()

    def on_mouse_scroll_down(self, ev):
        self.zoom_by(0.8)
        ev.stop()

    def selection_fractions(self):
        """Selection as (l, t, r, b) fractions of the displayed image, or None."""
        if self._sel is None or not self._dw or self._base is None:
            return None
        ux, uy = self._units()
        x0, y0, x1, y1 = self._norm_sel()
        x0 = max(x0, self._ox)
        y0 = max(y0, self._oy)
        x1 = min(x1, self._ox + self._dw - 1)
        y1 = min(y1, self._oy + self._dh - 1)
        if x1 - x0 + 1 < 2 * ux or y1 - y0 + 1 < 2 * uy:
            return None
        # canvas coords -> visible-window fractions -> whole-image fractions
        sx, sy, vw, vh = self._box
        w, h = self._img.size
        fx0 = (sx + (x0 - self._ox) / self._dw * vw) / w
        fy0 = (sy + (y0 - self._oy) / self._dh * vh) / h
        fx1 = (sx + (x1 - self._ox + 1) / self._dw * vw) / w
        fy1 = (sy + (y1 - self._oy + 1) / self._dh * vh) / h
        clamp = lambda v: min(1.0, max(0.0, v))
        return (clamp(fx0), clamp(fy0), clamp(fx1), clamp(fy1))

    def clear_selection(self):
        self._sel = None
        self._drag = None
        self._compose_canvas()
        self.refresh()


class GraphicsView(ImageView):
    """Image pane rendered with real pixels (kitty graphics protocol or sixel).

    Reuses all of ImageView's geometry/zoom/pan/crop logic, but the backing
    canvas is built at the terminal's true pixel resolution and displayed
    through a textual-image widget instead of half-blocks.
    """

    def __init__(self, widget_cls, cell_size):
        super().__init__()
        self._widget_cls = widget_cls
        self._cell = cell_size          # terminal cell size in pixels
        self._child = widget_cls()
        self._child.styles.width = "100%"
        self._child.styles.height = "100%"

    def compose(self) -> ComposeResult:
        yield self._child

    def _units(self):
        return (self._cell.width, self._cell.height)

    def _present(self, canvas):
        self._child.image = canvas


CURVE_COLORS = {"rgb": (225, 225, 225), "r": (235, 85, 85),
                "g": (90, 205, 95), "b": (95, 130, 255)}
CHANNELS = ("rgb", "r", "g", "b")


class CurvePlot(Widget):
    """Half-block canvas for the tone curve; mouse events go to the panel."""

    DEFAULT_CSS = "CurvePlot { width: 100%; height: 17; }"

    def __init__(self):
        super().__init__()
        self._grid = b""
        self._gw = self._gh = 0
        self._down = False

    def set_canvas(self, img):
        self._grid = img.tobytes()
        self._gw, self._gh = img.size
        self.refresh()

    def on_resize(self):
        self.parent.replot()

    def render_line(self, y):
        if not self._grid or y * 2 + 1 >= self._gh or self._gw > self.size.width:
            return Strip.blank(self.size.width)
        segs = []
        for x in range(self._gw):
            i = (y * 2 * self._gw + x) * 3
            k = ((y * 2 + 1) * self._gw + x) * 3
            segs.append(Segment("▀", Style(
                color=Color.from_rgb(self._grid[i], self._grid[i + 1], self._grid[i + 2]),
                bgcolor=Color.from_rgb(self._grid[k], self._grid[k + 1], self._grid[k + 2]))))
        return Strip(segs, self._gw)

    def _values(self, ev):
        """Event position as curve values (x, y) in 0..255, y pointing up."""
        r = self.region
        px = min(self._gw - 1, max(0, ev.screen_x - r.x))
        py = min(self._gh - 1, max(0, (ev.screen_y - r.y) * 2 + 1))
        return (round(px * 255 / max(1, self._gw - 1)),
                255 - round(py * 255 / max(1, self._gh - 1)))

    def on_mouse_down(self, ev: events.MouseDown):
        if not self._grid:
            return
        self.capture_mouse()
        self._down = True
        self.parent.plot_press(*self._values(ev))

    def on_mouse_move(self, ev: events.MouseMove):
        if self._down:
            self.parent.plot_drag(*self._values(ev))

    def on_mouse_up(self, ev: events.MouseUp):
        if self._down:
            self.release_mouse()
            self._down = False


class CurvePanel(Vertical, can_focus=True):
    """Interactive tone-curve editor; lives where the sidebar normally is."""

    DEFAULT_CSS = """
    CurvePanel { width: 42; height: 100%; padding: 1 2; background: $panel; display: none; }
    CurvePanel Static { height: auto; }
    """

    BINDINGS = [
        Binding("left", "move(-5, 0)", "Left", show=False),
        Binding("right", "move(5, 0)", "Right", show=False),
        Binding("up", "move(0, 5)", "Up", show=False),
        Binding("down", "move(0, -5)", "Down", show=False),
        Binding("shift+left", "move(-1, 0)", "Left fine", show=False),
        Binding("shift+right", "move(1, 0)", "Right fine", show=False),
        Binding("shift+up", "move(0, 1)", "Up fine", show=False),
        Binding("shift+down", "move(0, -1)", "Down fine", show=False),
        Binding("tab", "next_pt", "Next point", show=False),
        Binding("a", "add_pt", "Add point", show=False),
        Binding("backspace,delete", "del_pt", "Delete point", show=False),
        Binding("c", "channel", "Channel", show=False),
        Binding("r", "reset", "Reset channel", show=False),
        Binding("enter", "apply", "Apply", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    MAX_PTS = 10
    MIN_GAP = 5  # minimum x distance between control points

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.pts = {}
        self.ch = "rgb"
        self.sel = 0
        self._drag_idx = None

    def compose(self) -> ComposeResult:
        yield Static(id="curve-head")
        yield CurvePlot()
        yield Static(id="curve-foot")

    # -- open / close ---------------------------------------------------------

    def open_panel(self):
        self.pts = {ch: list(CURVE_IDENTITY) for ch in CHANNELS}
        self.ch, self.sel = "rgb", 0
        self.display = True
        self.app.query_one("#side").display = False
        self.focus()
        self.replot()

    def close_panel(self):
        self.display = False
        self.app.query_one("#side").display = True
        self.app.view.focus()

    def _pending_dict(self):
        return {ch: [list(p) for p in pts] for ch, pts in self.pts.items()
                if pts != CURVE_IDENTITY}

    def _changed(self):
        d = self._pending_dict()
        self.app.set_pending([("curves", d)] if d else [])
        self.replot()

    # -- rendering --------------------------------------------------------------

    def replot(self):
        if not self.display:
            return
        plot = self.query_one(CurvePlot)
        pw, ph = plot.size.width, plot.size.height * 2
        if pw < 8 or ph < 8:
            return
        img = Image.new("RGB", (pw, ph), (14, 14, 14))
        d = ImageDraw.Draw(img)
        for f in (0.25, 0.5, 0.75):
            d.line([(round(f * (pw - 1)), 0), (round(f * (pw - 1)), ph - 1)], fill=(34, 34, 34))
            d.line([(0, round(f * (ph - 1))), (pw - 1, round(f * (ph - 1)))], fill=(34, 34, 34))
        proc = getattr(self.app, "processed", None)
        if proc is not None:  # histogram of the active channel, behind everything
            h = proc.histogram()
            if self.ch == "rgb":
                chan = [h[k] + h[256 + k] + h[512 + k] for k in range(256)]
            else:
                i = "rgb".index(self.ch)
                chan = h[i * 256:(i + 1) * 256]
            per = 256 / pw
            edges = [int(c * per) for c in range(pw + 1)]
            buckets = [sum(chan[a:b]) / max(1, b - a) for a, b in zip(edges, edges[1:])]
            peak = max(buckets) or 1
            for c, bv in enumerate(buckets):
                bh = round((ph - 1) * bv / peak * 0.85)
                if bh:
                    d.line([(c, ph - 1), (c, ph - 1 - bh)], fill=(36, 44, 54))
        d.line([(0, ph - 1), (pw - 1, 0)], fill=(58, 58, 58))  # identity reference
        lut = monotone_lut(self.pts[self.ch])
        col = CURVE_COLORS[self.ch]
        to_px = lambda vx, vy: (round(vx * (pw - 1) / 255),
                                (ph - 1) - round(vy * (ph - 1) / 255))
        prev = None
        for c in range(pw):
            p = to_px(round(c * 255 / (pw - 1)), lut[round(c * 255 / (pw - 1))])
            if prev:
                d.line([prev, p], fill=col)
            prev = p
        for i, (vx, vy) in enumerate(self.pts[self.ch]):
            x, y = to_px(vx, vy)
            d.rectangle([x - 1, y - 1, x + 1, y + 1],
                        fill=(255, 210, 60) if i == self.sel else (240, 240, 240))
        plot.set_canvas(img)

        cname = {"rgb": "RGB", "r": "red", "g": "green", "b": "blue"}[self.ch]
        chip = {"rgb": "white", "r": "red", "g": "green", "b": "blue"}[self.ch]
        self.query_one("#curve-head", Static).update(
            f"[bold]curves[/] · [{chip}]{cname}[/] [dim](c to switch)[/]\n")
        x, y = self.pts[self.ch][self.sel]
        self.query_one("#curve-foot", Static).update(
            f"\npt {self.sel + 1}/{len(self.pts[self.ch])}: ({x}, {y})\n\n"
            "[dim]drag/arrows move · shift=fine · tab next\n"
            "a add · del remove · r reset channel\n"
            "enter apply · esc cancel[/]")

    # -- editing ------------------------------------------------------------------

    def _move_sel(self, nx, ny):
        pts = self.pts[self.ch]
        i = self.sel
        if i in (0, len(pts) - 1):          # endpoints: y only
            nx = pts[i][0]
        else:
            nx = min(max(nx, pts[i - 1][0] + self.MIN_GAP), pts[i + 1][0] - self.MIN_GAP)
        pts[i] = (nx, min(255, max(0, ny)))
        self._changed()

    def action_move(self, dx, dy):
        x, y = self.pts[self.ch][self.sel]
        self._move_sel(x + int(dx), y + int(dy))

    def action_next_pt(self):
        self.sel = (self.sel + 1) % len(self.pts[self.ch])
        self.replot()

    def action_add_pt(self, at_x=None, at_y=None):
        pts = self.pts[self.ch]
        if len(pts) >= self.MAX_PTS:
            return
        if at_x is None:  # keyboard: middle of the widest x gap
            gaps = [(pts[i + 1][0] - pts[i][0], i) for i in range(len(pts) - 1)]
            _, i = max(gaps)
            at_x = (pts[i][0] + pts[i + 1][0]) // 2
        at_x = int(at_x)
        if any(abs(at_x - px) < self.MIN_GAP for px, _ in pts):
            return
        at_y = monotone_lut(pts)[at_x] if at_y is None else int(at_y)
        pts.append((at_x, at_y))
        pts.sort()
        self.sel = pts.index((at_x, at_y))
        self._changed()

    def action_del_pt(self):
        pts = self.pts[self.ch]
        if 0 < self.sel < len(pts) - 1:
            pts.pop(self.sel)
            self.sel = min(self.sel, len(pts) - 1)
            self._changed()

    def action_channel(self):
        self.ch = CHANNELS[(CHANNELS.index(self.ch) + 1) % len(CHANNELS)]
        self.sel = 0
        self.replot()

    def action_reset(self):
        self.pts[self.ch] = list(CURVE_IDENTITY)
        self.sel = 0
        self._changed()

    def action_apply(self):
        d = self._pending_dict()
        self.app.set_pending([])
        if d:
            self.app._push(("curves", d))
        self.close_panel()

    def action_cancel(self):
        self.app.set_pending([])
        self.close_panel()

    # -- mouse from the plot -----------------------------------------------------

    def plot_press(self, vx, vy):
        pts = self.pts[self.ch]
        dists = [max(abs(vx - px), abs(vy - py)) for px, py in pts]
        best = min(range(len(pts)), key=lambda i: dists[i])
        if dists[best] <= 24:               # grab an existing point
            self.sel = best
            self._drag_idx = best
            self.replot()
        else:                               # add a new one and drag it
            before = len(pts)
            self.action_add_pt(vx, vy)
            self._drag_idx = self.sel if len(pts) > before else None

    def plot_drag(self, vx, vy):
        if self._drag_idx is not None:
            self._move_sel(vx, vy)


class FileListScreen(ModalScreen):
    """Pick a photo from the current directory; edited ones are starred."""

    DEFAULT_CSS = """
    FileListScreen { align: center middle; }
    FileListScreen OptionList { width: 64; max-height: 24; border: tall $accent; }
    """
    BINDINGS = [Binding("escape", "dismiss_none", show=False)]

    def __init__(self, labels, current):
        super().__init__()
        self._labels = labels
        self._current = current

    def compose(self) -> ComposeResult:
        yield OptionList(*self._labels)

    def on_mount(self):
        ol = self.query_one(OptionList)
        ol.highlighted = self._current
        ol.focus()

    def on_option_list_option_selected(self, ev: OptionList.OptionSelected):
        self.dismiss(ev.option_index)

    def action_dismiss_none(self):
        self.dismiss(None)


class ExportScreen(ModalScreen):
    """Export with options: path, max long side, quality."""

    DEFAULT_CSS = """
    ExportScreen { align: center middle; }
    ExportScreen > Vertical { width: 64; height: auto; padding: 1 2;
                              background: $panel; border: tall $accent; }
    ExportScreen Input { margin-bottom: 1; }
    ExportScreen Static { height: auto; }
    """
    BINDINGS = [Binding("escape", "dismiss_none", show=False)]

    def __init__(self, default_name):
        super().__init__()
        self._default = default_name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]export[/]\n")
            yield Input(value=self._default, placeholder="output path", id="exp-path")
            yield Input(placeholder="max long side in px (empty = full size)", id="exp-size")
            yield Input(value="95", placeholder="quality (jpeg/webp)", id="exp-q")
            yield Static("[dim]enter export · esc cancel[/]")

    def on_input_submitted(self, ev: Input.Submitted):
        path = self.query_one("#exp-path", Input).value.strip()
        size = self.query_one("#exp-size", Input).value.strip()
        q = self.query_one("#exp-q", Input).value.strip()
        if not path:
            return
        self.dismiss({"path": path,
                      "max_side": int(size) if size.isdigit() else None,
                      "quality": int(q) if q.isdigit() else 95})

    def action_dismiss_none(self):
        self.dismiss(None)


class SaveAsScreen(ModalScreen):

    DEFAULT_CSS = """
    SaveAsScreen { align: center middle; }
    SaveAsScreen Input { width: 60; border: tall $accent; }
    """
    BINDINGS = [Binding("escape", "dismiss_none", show=False)]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="save as... (e.g. out.png)")

    def on_input_submitted(self, ev: Input.Submitted):
        self.dismiss(ev.value.strip() or None)

    def action_dismiss_none(self):
        self.dismiss(None)


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------

HELP = """\
[bold]crop[/]      c  drag, enter=apply esc=cancel, x=aspect
[bold]rotate[/]    r / e        [bold]flip[/]  f / v
[bold]level[/]     , / . tilt 0.5°   < / > fine 0.1°
[bold]bright[/]    B + / b -    [bold]contrast[/]  N + / n -
[bold]satur.[/]    S + / s -    [bold]sharp[/]     P + / p -
[bold]tone[/]      D/d gamma   H/h shadows   J/j highlights
[bold]color[/]     T/t warm/cool   M/m magenta/green
[bold]curves[/]    k tone curve editor (RGB + channels)
[bold]filters[/]   g gray  a autocontrast  u blur  i sharpen
[bold]zoom[/]      + / - or wheel   0 fit
[bold]pan[/]       arrows, or drag when zoomed
[bold]files[/]     [ / ] prev/next photo   o browse
[bold]compare[/]   \\ toggle original
[bold]undo/redo[/] z / y
[bold]clipboard[/] Y copy out   V paste in
[bold]save[/]      ctrl+s copy  w save as  W export  q quit\
"""


class TermShop(App):

    TITLE = "termshop"
    CSS = """
    #main { layout: horizontal; height: 100%; }
    ImageView { width: 1fr; height: 100%; }
    #side { width: 42; height: 100%; padding: 1 2; background: $panel; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("c", "crop", "Crop"),
        Binding("r", "apply('rotate_cw')", "Rotate"),
        Binding("e", "apply('rotate_ccw')", "Rotate CCW", show=False),
        Binding("f", "apply('flip_h')", "Flip", show=False),
        Binding("v", "apply('flip_v')", "VFlip", show=False),
        Binding("B", "apply('brightness', 1.08)", "Bright+", show=False),
        Binding("b", "apply('brightness', 0.92)", "Bright-", show=False),
        Binding("N", "apply('contrast', 1.08)", "Contrast+", show=False),
        Binding("n", "apply('contrast', 0.92)", "Contrast-", show=False),
        Binding("S", "apply('saturation', 1.1)", "Sat+", show=False),
        Binding("s", "apply('saturation', 0.9)", "Sat-", show=False),
        Binding("P", "apply('sharpness', 1.2)", "Sharp+", show=False),
        Binding("p", "apply('sharpness', 0.8)", "Sharp-", show=False),
        Binding("D", "apply('gamma', 1.1)", "Gamma+", show=False),
        Binding("d", "apply('gamma', 0.9)", "Gamma-", show=False),
        Binding("H", "apply('shadows', 0.08)", "Shadows+", show=False),
        Binding("h", "apply('shadows', -0.08)", "Shadows-", show=False),
        Binding("J", "apply('highlights', 0.08)", "Highlights+", show=False),
        Binding("j", "apply('highlights', -0.08)", "Highlights-", show=False),
        Binding("T", "apply('temp', 0.06)", "Warmer", show=False),
        Binding("t", "apply('temp', -0.06)", "Cooler", show=False),
        Binding("M", "apply('tint', 0.06)", "Magenta", show=False),
        Binding("m", "apply('tint', -0.06)", "Green", show=False),
        Binding("x", "aspect", "Crop aspect", show=False),
        Binding("backslash", "toggle_original", "Compare", show=False),
        Binding("right_square_bracket", "nav(1)", "Next photo", show=False),
        Binding("left_square_bracket", "nav(-1)", "Prev photo", show=False),
        Binding("o", "browse", "Browse", show=False),
        Binding("k", "curves", "Curves", show=False),
        Binding("full_stop", "apply('angle', 0.5)", "Tilt right", show=False),
        Binding("comma", "apply('angle', -0.5)", "Tilt left", show=False),
        Binding("greater_than_sign", "apply('angle', 0.1)", "Tilt right fine", show=False),
        Binding("less_than_sign", "apply('angle', -0.1)", "Tilt left fine", show=False),
        Binding("W", "export", "Export", show=False),
        Binding("Y", "copy", "Copy to clipboard", show=False),
        Binding("V", "paste", "Paste from clipboard", show=False),
        Binding("g", "apply('grayscale')", "Gray", show=False),
        Binding("a", "apply('autocontrast')", "Auto", show=False),
        Binding("u", "apply('blur', 2.0)", "Blur", show=False),
        Binding("i", "apply('sharpen', 120)", "Sharpen", show=False),
        Binding("plus,equals_sign", "zoom(1.25)", "Zoom+", show=False),
        Binding("minus", "zoom(0.8)", "Zoom-", show=False),
        Binding("0", "zoom_reset", "Fit", show=False),
        Binding("left", "pan(-1, 0)", "Pan left", show=False),
        Binding("right", "pan(1, 0)", "Pan right", show=False),
        Binding("up", "pan(0, -1)", "Pan up", show=False),
        Binding("down", "pan(0, 1)", "Pan down", show=False),
        Binding("z", "undo", "Undo"),
        Binding("y", "redo", "Redo", show=False),
        Binding("ctrl+s", "save", "Save"),
        Binding("w", "saveas", "Save as", show=False),
        Binding("enter", "apply_crop", "Apply crop", show=False),
        Binding("escape", "cancel_crop", "Cancel crop", show=False),
    ]

    def __init__(self, path, renderer=None):
        """path: an image file or a directory of images.
        renderer: None (half-blocks), 'tgp' (kitty graphics) or 'sixel'."""
        super().__init__()
        path = Path(path)
        if path.is_dir():
            self.files = list_images(path)
            start = self.files[0]
        else:
            start = path.resolve()
            siblings = list_images(path.parent)
            self.files = siblings if start in siblings else [start]
        self.file_idx = self.files.index(start)
        self._pending = []          # preview-only ops (live curve editing)
        self.sessions = {}          # path -> (ops, redo_stack), per-image history
        self.sidecar = start.parent / SIDECAR_NAME
        by_name = {p.name: p for p in self.files}
        for name, ops in load_sidecar(self.sidecar).items():
            if name in by_name:
                self.sessions[by_name[name]] = (ops, [])
        self._load(start)
        self.renderer = renderer
        self._gfx = None
        if renderer:
            # Import must happen before the app starts: textual-image queries the
            # terminal for its cell pixel size on import, which is impossible once
            # Textual owns stdin/stdout.
            from textual_image.widget import SixelImage, TGPImage, get_cell_size
            cls = TGPImage if renderer == "tgp" else SixelImage
            self._gfx = (cls, get_cell_size())

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield GraphicsView(*self._gfx) if self._gfx else ImageView()
            yield Static(id="side")
            yield CurvePanel()
        yield Footer()

    def on_mount(self):
        self.view = self.query_one(ImageView)
        self.view.focus()
        self._recompute()
        restored = sum(1 for _, (ops, _) in self.sessions.items() if ops)
        if restored:
            self.notify(f"restored unsaved edits for {restored} photo(s)")

    # -- file loading / navigation ---------------------------------------------

    def _load(self, path):
        self.path = Path(path)
        img = ImageOps.exif_transpose(Image.open(self.path)).convert("RGB")
        self.original = img
        self.preview = img.copy()
        self.preview.thumbnail((PREVIEW_MAX, PREVIEW_MAX), Image.Resampling.LANCZOS)
        self.ops, self.redo_stack = self.sessions.get(self.path, ([], []))
        self.show_original = False

    def _switch_to(self, idx):
        panel = self.query_one(CurvePanel)
        if panel.display:
            panel.action_cancel()
        self.sessions[self.path] = (self.ops, self.redo_stack)
        try:
            self._load(self.files[idx])
        except (OSError, ValueError) as exc:
            self.notify(f"cannot open {self.files[idx].name}: {exc}", severity="error")
            self._load(self.path)  # stay where we were
            return
        self.file_idx = idx
        self.view.crop_mode = False
        self.view.clear_selection()
        self.view.reset_view()
        self._recompute()

    def action_nav(self, delta):
        if len(self.files) > 1:
            self._switch_to((self.file_idx + int(delta)) % len(self.files))

    def action_browse(self):
        edited = {p for p, (ops, _) in self.sessions.items() if ops}
        if self.ops:
            edited.add(self.path)
        labels = [("* " if f in edited else "  ") + f.name for f in self.files]

        def done(idx):
            if idx is not None and idx != self.file_idx:
                self._switch_to(idx)
        self.push_screen(FileListScreen(labels, self.file_idx), done)

    # -- pipeline ------------------------------------------------------------

    def _recompute(self):
        self.show_original = False
        img = self.preview
        for op in self.ops + self._pending:
            img = apply_op(img, op)
        self.processed = img
        self.view.set_image(img)
        self._update_side()
        self._persist()

    def set_pending(self, ops):
        """Preview-only ops (live curve editing); never persisted or saved."""
        self._pending = ops
        self._recompute()

    def _persist(self):
        """Mirror all in-memory pipelines to the directory's sidecar file."""
        edits = {p.name: ops for p, (ops, _) in self.sessions.items() if ops}
        if self.ops:
            edits[self.path.name] = self.ops
        else:
            edits.pop(self.path.name, None)
        write_sidecar(self.sidecar, edits)

    def _push(self, op):
        if op[0] == "angle" and self.ops and self.ops[-1][0] == "angle":
            # merge consecutive tilt nudges into one op (one undo, one resample)
            merged = round(self.ops[-1][1] + op[1], 2)
            self.ops.pop()
            if abs(merged) >= 0.05:
                self.ops.append(("angle", merged))
        else:
            self.ops.append(op)
        self.redo_stack.clear()
        self._recompute()

    def full_size(self):
        return reduce(op_size, self.ops, self.original.size)

    def _update_side(self):
        w, h = self.full_size()
        view = getattr(self, "view", None)
        zoom = f"   [yellow]zoom {view._zoom:g}x[/]" if view and view._zoom > 1 else ""
        lines = [
            f"[bold cyan]termshop[/] [dim]v0.2 · {self.renderer or 'half-block'} renderer[/]",
            "",
            f"[bold]{escape(self.path.name)}[/]"
            + (f"  [dim]({self.file_idx + 1}/{len(self.files)})[/]" if len(self.files) > 1 else ""),
            f"{self.original.width}x{self.original.height}  ->  [bold]{w}x{h}[/]{zoom}",
            "[black on yellow] SHOWING ORIGINAL [/]" if self.show_original else "",
        ]
        shown = getattr(self, "processed", None)
        if shown is not None:
            hist = (self.preview if self.show_original else shown).histogram()
            lines += [
                f"[red]{spark_hist(hist[0:256])}[/]",
                f"[green]{spark_hist(hist[256:512])}[/]",
                f"[blue]{spark_hist(hist[512:768])}[/]",
                "",
            ]
        lines += [
            f"[bold]history[/] [dim]({len(self.ops)} ops)[/]",
        ]
        shown = self.ops[-10:]
        if len(self.ops) > 10:
            lines.append(f"  [dim]... {len(self.ops) - 10} earlier[/]")
        for i, op in enumerate(shown, len(self.ops) - len(shown) + 1):
            lines.append(f"  {i}. {op_label(op)}")
        if not self.ops:
            lines.append("  [dim](no edits yet)[/]")
        lines += ["", HELP]
        try:
            self.query_one("#side", Static).update("\n".join(lines))
        except Exception:
            pass  # e.g. a modal screen is on top

    # -- actions ---------------------------------------------------------------

    def action_apply(self, name, arg=None):
        self._push((name, arg))

    def action_zoom(self, factor):
        self.view.zoom_by(factor)

    def action_zoom_reset(self):
        self.view.reset_view()

    def action_pan(self, dx, dy):
        self.view.pan_by(dx * 0.15, dy * 0.15)

    def action_undo(self):
        if self.ops:
            self.redo_stack.append(self.ops.pop())
            self._recompute()

    def action_redo(self):
        if self.redo_stack:
            self.ops.append(self.redo_stack.pop())
            self._recompute()

    def action_curves(self):
        panel = self.query_one(CurvePanel)
        if panel.display:
            panel.action_cancel()
        else:
            if self.view.crop_mode:
                self.action_cancel_crop()
            panel.open_panel()

    def action_toggle_original(self):
        if not self.ops:
            return
        self.show_original = not self.show_original
        self.view.set_image(self.preview if self.show_original else self.processed)
        self._update_side()

    def action_aspect(self):
        if self.view.crop_mode:
            self.notify(f"crop aspect: {self.view.cycle_aspect()}")

    def action_crop(self):
        self.view.crop_mode = not self.view.crop_mode
        if self.view.crop_mode:
            self.notify("crop: drag to select, enter=apply, esc=cancel, x=aspect ratio")
        else:
            self.view.clear_selection()

    def action_apply_crop(self):
        if not self.view.crop_mode:
            return
        fr = self.view.selection_fractions()
        if fr is None:
            self.notify("no selection -- drag over the image first", severity="warning")
            return
        self.view.crop_mode = False
        self.view.clear_selection()
        self._push(("crop", fr))

    def action_cancel_crop(self):
        if self.view.crop_mode:
            self.view.crop_mode = False
            self.view.clear_selection()

    # -- saving ----------------------------------------------------------------

    def _render_full(self):
        img = self.original
        for op in self.ops:
            img = apply_op(img, op)
        return img

    def _save_to(self, path, max_side=None, quality=95):
        path = Path(path).expanduser()
        img = self._render_full()
        if max_side:
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        kwargs = {"quality": quality} if path.suffix.lower() in (".jpg", ".jpeg", ".webp") else {}
        try:
            img.save(path, **kwargs)
        except (ValueError, OSError) as exc:
            self.notify(f"save failed: {exc}", severity="error")
            return
        self.notify(f"saved {path.name}  ({img.width}x{img.height})")

    def action_save(self):
        out = self.path.with_name(self.path.stem + "_edited" + self.path.suffix)
        self._save_to(out)

    def action_saveas(self):
        def done(value):
            if value:
                self._save_to(value)
        self.push_screen(SaveAsScreen(), done)

    def action_export(self):
        default = self.path.stem + "_export" + self.path.suffix

        def done(cfg):
            if cfg:
                self._save_to(cfg["path"], max_side=cfg["max_side"], quality=cfg["quality"])
        self.push_screen(ExportScreen(default), done)

    # -- clipboard ---------------------------------------------------------------

    def action_copy(self):
        import tempfile
        img = self._render_full()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp = Path(f.name)
        try:
            img.save(tmp)
            ok = clipboard_copy_png(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        if ok:
            self.notify(f"copied to clipboard ({img.width}x{img.height})")
        else:
            self.notify("clipboard copy failed (no supported clipboard tool)",
                        severity="warning")

    def action_paste(self):
        data, err = clipboard_paste_png()
        if not data:
            self.notify(f"paste: {err}", severity="warning")
            return
        out = self.path.parent / time.strftime("pasted_%Y%m%d_%H%M%S.png")
        try:
            out.write_bytes(data)
        except OSError as exc:
            self.notify(f"paste failed: {exc}", severity="error")
            return
        self.files.append(out.resolve())
        self._switch_to(len(self.files) - 1)
        self.notify(f"pasted clipboard -> {out.name}")


# --------------------------------------------------------------------------
# Batch CLI: export sidecar edits (--commit) or apply one pipeline to many
# files (--apply) without launching the TUI.
# --------------------------------------------------------------------------

def render_pipeline(path, ops):
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    for op in ops:
        img = apply_op(img, op)
    return img


def save_image(img, path, quality=95):
    kwargs = {"quality": quality} if path.suffix.lower() in (".jpg", ".jpeg", ".webp") else {}
    img.save(path, **kwargs)


def load_pipeline(src):
    """Pipeline for --apply: a .json ops file, or an image with sidecar edits."""
    src = Path(src)
    if src.suffix.lower() == ".json":
        try:
            data = json.loads(src.read_text())
        except (OSError, ValueError) as exc:
            raise SystemExit(f"error: cannot read {src}: {exc}")
        if isinstance(data, list):
            ops = clean_ops(data)
        else:
            edits = data.get("edits", {}) if isinstance(data, dict) else {}
            if len(edits) != 1:
                raise SystemExit(
                    f"error: {src} holds {len(edits)} pipelines -- "
                    "pass the edited image's path instead to pick one")
            ops = clean_ops(next(iter(edits.values())))
    else:
        ops = load_sidecar(src.parent / SIDECAR_NAME).get(src.name, [])
    if not ops:
        raise SystemExit(f"error: no usable edits in {src}")
    return ops


def run_batch(args):
    suffix = args.suffix if args.suffix is not None else ("" if args.outdir else "_edited")
    outdir = Path(args.outdir) if args.outdir else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
    targets = [Path(p) for p in args.paths]
    if not targets:
        raise SystemExit("error: no input files given")

    jobs = []  # (source image, ops)
    if args.apply:
        ops = load_pipeline(args.apply)
        for t in targets:
            if not t.exists():
                print(f"skip {t}: not found")
                continue
            jobs += [(f, ops) for f in (list_images(t) if t.is_dir() else [t.resolve()])]
    else:  # --commit
        for t in targets:
            if t.is_dir():
                edits = load_sidecar(t / SIDECAR_NAME)
                jobs += [(t / n, o) for n, o in sorted(edits.items()) if (t / n).exists()]
            elif t.exists():
                ops = load_sidecar(t.parent / SIDECAR_NAME).get(t.name)
                if ops:
                    jobs.append((t.resolve(), ops))
                else:
                    print(f"skip {t.name}: no saved edits")
            else:
                print(f"skip {t}: not found")

    done, exported = 0, {}
    for src, ops in jobs:
        out = (outdir or src.parent) / f"{src.stem}{suffix}{src.suffix}"
        if out.resolve() == src.resolve():
            print(f"skip {src.name}: output would overwrite input (use -o or --suffix)")
            continue
        try:
            img = render_pipeline(src, ops)
            if args.max_side:
                img.thumbnail((args.max_side, args.max_side), Image.Resampling.LANCZOS)
            save_image(img, out, args.quality)
        except (OSError, ValueError) as exc:
            print(f"error {src.name}: {exc}")
            continue
        print(f"{src.name} -> {out}  ({img.width}x{img.height}, {len(ops)} ops)")
        exported.setdefault(src.parent, set()).add(src.name)
        done += 1

    if args.clear:
        for d, names in exported.items():
            edits = load_sidecar(d / SIDECAR_NAME)
            for n in names:
                edits.pop(n, None)
            write_sidecar(d / SIDECAR_NAME, edits)

    if not done:
        raise SystemExit("nothing to export")
    print(f"done: {done} file(s)")


def resolve_renderer(choice):
    """Map a --renderer choice to a concrete backend (None = half-blocks)."""
    if choice == "half":
        return None
    if choice in ("tgp", "sixel"):
        return choice
    # auto: textual-image probes the terminal on import (pre-app, so it's safe)
    try:
        from textual_image.renderable import Image as Auto
        from textual_image.renderable.sixel import Image as Sixel
        from textual_image.renderable.tgp import Image as TGP
    except Exception:
        return None
    if Auto is TGP:
        return "tgp"
    if Auto is Sixel:
        return "sixel"
    return None


def web_main():
    """Serve termshop to a web browser (needs the 'web' extra: textual-serve)."""
    import argparse
    import shlex
    ap = argparse.ArgumentParser(
        prog="termshop-web", description="run termshop in a web browser")
    ap.add_argument("image", help="image file or directory to edit")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--public-url", default=None,
                    help="external origin when served behind a proxy/tunnel "
                         "(e.g. https://demo.example.com) -- fixes asset and websocket URLs")
    args = ap.parse_args()
    path = Path(args.image).resolve()
    if not path.exists():
        print(f"error: {path} not found")
        sys.exit(1)
    try:
        from textual_serve.server import Server
    except ImportError:
        print("error: needs textual-serve -- install with:\n"
              "  pip install 'termshop[web]'   (or: pip install textual-serve)")
        sys.exit(1)
    # Web terminals speak rich text, not kitty graphics: force half-blocks.
    cmd = f"{shlex.quote(sys.executable)} -m termshop {shlex.quote(str(path))} --renderer half"
    print(f"serving termshop at http://{args.host}:{args.port}  (ctrl+c to stop)")
    Server(cmd, host=args.host, port=args.port, title="termshop",
           public_url=args.public_url).serve()


def main():
    import argparse
    ap = argparse.ArgumentParser(
        prog="termshop", description="a photo editor that lives in your terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  termshop photo.jpg                    edit one photo\n"
               "  termshop ~/Pictures                   browse a directory\n"
               "  termshop --commit ~/Pictures          export all sidecar edits\n"
               "  termshop --commit ~/Pictures --clear  ...and mark them done\n"
               "  termshop --apply best.jpg *.jpg       copy best.jpg's edits to others\n"
               "  termshop --apply ops.json pics/ -o out/   apply a pipeline file")
    ap.add_argument("paths", nargs="*", help="image file(s) or directory")
    ap.add_argument("--renderer", choices=("auto", "tgp", "sixel", "half"), default="auto",
                    help="auto-detects kitty/ghostty graphics or sixel; "
                         "'half' forces the universal half-block renderer")
    ap.add_argument("--commit", action="store_true",
                    help="batch: render saved sidecar edits to files and exit")
    ap.add_argument("--apply", metavar="SRC",
                    help="batch: apply SRC's pipeline (a .json ops file, or an "
                         "image with sidecar edits) to the given files")
    ap.add_argument("-o", "--outdir", help="batch: write outputs here")
    ap.add_argument("--suffix", help="batch: output name suffix "
                                     "(default: '_edited', or '' with --outdir)")
    ap.add_argument("--quality", type=int, default=95, help="batch: JPEG quality")
    ap.add_argument("--max-side", type=int,
                    help="batch: downscale outputs so the long side is at most this")
    ap.add_argument("--clear", action="store_true",
                    help="batch: remove exported entries from the sidecar")
    args = ap.parse_args()

    if args.commit and args.apply:
        ap.error("--commit and --apply are mutually exclusive")
    if args.commit or args.apply:
        run_batch(args)
        return
    for flag, name in ((args.outdir, "-o"), (args.suffix, "--suffix"),
                       (args.clear, "--clear"), (args.max_side, "--max-side")):
        if flag:
            ap.error(f"{name} only makes sense with --commit or --apply")

    if len(args.paths) != 1:
        ap.error("interactive mode takes exactly one image or directory")
    path = Path(args.paths[0])
    if not path.exists():
        print(f"error: {path} not found")
        sys.exit(1)
    if path.is_dir() and not list_images(path):
        print(f"error: no images found in {path}")
        sys.exit(1)
    TermShop(path, renderer=resolve_renderer(args.renderer)).run()


if __name__ == "__main__":
    main()
