"""Headless test of the GraphicsView (TGP backend, fallback 10x20 cell size)."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
import asyncio
import sys
from termshop import TermShop, GraphicsView

from pathlib import Path as _P; _P(".termshop.json").unlink(missing_ok=True)
async def run():
    app = TermShop("sample.jpg", renderer="tgp")
    async with app.run_test(size=(110, 32)) as pilot:
        v = app.view
        assert isinstance(v, GraphicsView), type(v)
        ux, uy = v._units()
        assert (ux, uy) == (10, 20), (ux, uy)  # headless fallback cell size
        # canvas is at true pixel resolution
        assert v._base.size == (v.size.width * ux, v.size.height * uy), v._base.size
        # child widget received the canvas
        assert v._child.image is not None and v._child.image.size == v._base.size
        # full-pane selection -> full image fractions, same math as half-block
        v.crop_mode = True
        v._sel = (v._ox, v._oy, v._ox + v._dw - 1, v._oy + v._dh - 1)
        assert v.selection_fractions() == (0.0, 0.0, 1.0, 1.0), v.selection_fractions()
        # selection overlay is baked into the presented canvas (corner dimmed)
        v._sel = (v._ox + v._dw // 2, v._oy, v._ox + v._dw - 1, v._oy + v._dh - 1)
        v._compose_canvas()
        presented = v._child.image
        lx, ly = v._ox + 2, v._oy + v._dh // 2          # far left: outside sel -> dimmed
        rx = v._ox + v._dw - 4                           # far right: inside sel
        assert sum(presented.getpixel((lx, ly))) < sum(v._base.getpixel((lx, ly))), "outside not dimmed"
        assert presented.getpixel((rx, ly)) == v._base.getpixel((rx, ly)), "inside altered"
        # zoom works through the same path
        await pilot.press("plus")
        assert v._zoom == 1.25 and v._box[2] < v._img.size[0]
        app.action_apply_crop()
        await pilot.pause()
        assert app.ops and app.ops[-1][0] == "crop"
    print("PASS: graphics view geometry/overlay/zoom ok", file=sys.stderr)

asyncio.run(run())
