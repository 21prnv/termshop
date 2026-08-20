"""Tests for tone/color ops, aspect-locked crop, and before/after toggle."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
import asyncio
from textual.geometry import Offset
from PIL import Image, ImageStat
from termshop import TermShop, apply_op

# --- op math (no app needed) ------------------------------------------------
from pathlib import Path as _P; _P(".termshop.json").unlink(missing_ok=True)
img = Image.open("sample.jpg").convert("RGB")
mean = lambda i: ImageStat.Stat(i).mean

m0 = mean(img)
warm = mean(apply_op(img, ("temp", 0.06)))
assert warm[0] > m0[0] and warm[2] < m0[2], "temp+ should raise R, lower B"
cool = mean(apply_op(img, ("temp", -0.06)))
assert cool[0] < m0[0] and cool[2] > m0[2]

magenta = mean(apply_op(img, ("tint", 0.06)))
assert magenta[1] < m0[1] and magenta[0] > m0[0], "tint+ should lower G"

bright = mean(apply_op(img, ("gamma", 1.1)))
assert sum(bright) > sum(m0), "gamma>1 brightens"

dark_region = img.point(lambda v: v // 4)  # mostly shadows
lifted = mean(apply_op(dark_region, ("shadows", 0.08)))
assert sum(lifted) > sum(mean(dark_region)), "shadows+ lifts dark pixels"
hi = apply_op(img, ("highlights", -0.08))
assert sum(mean(hi)) < sum(m0), "highlights- pulls bright end down"
print("PASS: tone/color op math")

# --- app behavior -------------------------------------------------------------
async def run():
    app = TermShop("sample.jpg")
    async with app.run_test(size=(110, 32)) as pilot:
        v = app.view
        # aspect-locked crop: cycle to 1:1, drag, selection must be square
        await pilot.press("c", "x")
        assert v.aspect == 1.0, v.aspect
        await pilot.mouse_down(v, Offset(10, 5))
        await pilot.hover(v, Offset(50, 12))
        await pilot.mouse_up(v, Offset(50, 12))
        x0, y0, x1, y1 = v._norm_sel()
        assert abs((x1 - x0) - (y1 - y0)) <= 1, f"not square: {x1-x0}x{y1-y0}"
        await pilot.press("escape")

        # x outside crop mode does nothing
        idx_before = v._aspect_idx
        await pilot.press("x")
        assert v._aspect_idx == idx_before

        # before/after: no-op with no edits, then toggles, then resets on edit
        await pilot.press("backslash")
        assert not app.show_original, "toggle should no-op with no ops"
        await pilot.press("B", "B")
        await pilot.press("backslash")
        assert app.show_original and v._img is app.preview
        await pilot.press("backslash")
        assert not app.show_original and v._img is app.processed
        await pilot.press("backslash")   # showing original...
        await pilot.press("T")           # new edit snaps back to edited view
        assert not app.show_original and v._img is app.processed
        assert app.ops[-1] == ("temp", 0.06)
    print("PASS: aspect lock + before/after")

asyncio.run(run())
