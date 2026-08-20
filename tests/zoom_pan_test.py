"""Test zoom/pan: window shrinks, pans clamp, crop maps correctly while zoomed."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
import asyncio
from termshop import TermShop

from pathlib import Path as _P; _P(".termshop.json").unlink(missing_ok=True)
async def run():
    app = TermShop("sample.jpg")
    async with app.run_test(size=(110, 32)) as pilot:
        v = app.view
        w, h = v._img.size
        assert v._zoom == 1.0 and v._box[2] == w  # fit: full width visible

        await pilot.press("plus", "plus")          # zoom to 1.5625x
        assert abs(v._zoom - 1.5625) < 1e-9, v._zoom
        sx0, sy0, vw0, vh0 = v._box
        assert vw0 < w, "visible window should shrink when zoomed"

        await pilot.press("right")                 # pan right moves window right
        assert v._box[0] > sx0, (v._box[0], sx0)

        for _ in range(60):                        # pan hard left -> clamps at 0
            await pilot.press("left")
        assert v._box[0] == 0, v._box

        # crop while zoomed: full-pane selection == visible window fractions
        v.crop_mode = True
        v._sel = (v._ox, v._oy, v._ox + v._dw - 1, v._oy + v._dh - 1)
        l, t, r, b = v.selection_fractions()
        sx, sy, vw, vh = v._box
        assert abs(l - sx / w) < 0.01 and abs(r - (sx + vw) / w) < 0.01, (l, r, sx, vw)
        assert r < 1.0, "zoomed crop must not span the whole image"
        app.action_apply_crop()
        await pilot.pause()
        fw, fh = app.full_size()
        assert fw < 640 and app.ops[-1][0] == "crop"

        await pilot.press("z", "0")                # undo crop, reset view
        assert v._zoom == 1.0 and v._box[0] == 0 and v._box[2] == v._img.size[0]

        # mouse wheel zooms
        v.on_mouse_scroll_up(type("E", (), {"stop": lambda s: None})())
        assert v._zoom == 1.25
    print("PASS: zoom/pan/crop-at-zoom ok")

asyncio.run(run())
