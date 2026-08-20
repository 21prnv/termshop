"""Verify half-block rendering produces real pixels and crop math maps correctly."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
import asyncio
from termshop import TermShop

from pathlib import Path as _P; _P(".termshop.json").unlink(missing_ok=True)
async def run():
    app = TermShop("sample.jpg")
    async with app.run_test(size=(110, 32)) as pilot:
        view = app.view
        strip = view.render_line(view.size.height // 2)  # middle row
        colored = [s for s in strip._segments if s.style and s.style.color]
        assert len(colored) > 50, f"only {len(colored)} colored cells"
        # crop selection: select the full displayed image -> fractions ~ (0,0,1,1)
        view.crop_mode = True
        view._sel = (view._ox, view._oy, view._ox + view._dw - 1, view._oy + view._dh - 1)
        l, t, r, b = view.selection_fractions()
        assert (l, t) == (0.0, 0.0) and (r, b) == (1.0, 1.0), (l, t, r, b)
        # half selection maps to ~0.5
        view._sel = (view._ox, view._oy, view._ox + view._dw // 2, view._oy + view._dh - 1)
        fr = view.selection_fractions()
        assert abs(fr[2] - 0.5) < 0.02, fr
        # apply via the real action path
        app.action_apply_crop()
        await pilot.pause()
        assert app.ops[-1][0] == "crop" and not view.crop_mode
    print("PASS: rendering + crop mapping ok")

asyncio.run(run())
