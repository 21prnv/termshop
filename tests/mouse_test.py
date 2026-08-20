"""Verify real mouse events drive crop selection (screen-coord conversion)."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
import asyncio
from textual.geometry import Offset
from termshop import TermShop

from pathlib import Path as _P; _P(".termshop.json").unlink(missing_ok=True)
async def run():
    app = TermShop("sample.jpg")
    async with app.run_test(size=(110, 32)) as pilot:
        v = app.view
        await pilot.press("c")                 # crop mode
        assert v.crop_mode
        await pilot.mouse_down(v, Offset(10, 5))
        await pilot.hover(v, Offset(40, 20))   # fires MouseMove while captured
        await pilot.mouse_up(v, Offset(40, 20))
        assert v._sel is not None, "selection not created by mouse"
        x0, y0, x1, y1 = v._norm_sel()
        assert (x0, y0) == (10, 10) and x1 >= 40, (x0, y0, x1, y1)  # y*2 units
        fr = v.selection_fractions()
        assert fr is not None
        await pilot.press("enter")
        assert app.ops and app.ops[-1][0] == "crop", app.ops
    print("PASS: mouse-driven crop ok")

asyncio.run(run())
