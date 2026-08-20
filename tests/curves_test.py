"""Tests for the curves editor: math, op, sidecar round-trip, panel interaction."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
from pathlib import Path as _P; _P(".termshop.json").unlink(missing_ok=True)
import asyncio
import json
from PIL import Image, ImageStat
from termshop import (TermShop, CurvePanel, apply_op, clean_ops, monotone_lut,
                      CURVE_IDENTITY)

# --- monotone_lut math ---------------------------------------------------------
ident = monotone_lut(CURVE_IDENTITY)
assert ident == list(range(256)), "identity points must give identity LUT"

lifted = monotone_lut([(0, 0), (128, 190), (255, 255)])
assert lifted[128] == 190
assert all(b >= a for a, b in zip(lifted, lifted[1:])), "must stay monotone"
assert max(lifted) <= 255 and min(lifted) >= 0, "no overshoot (Fritsch-Carlson)"

sfla = monotone_lut([(0, 100), (100, 100), (200, 200), (255, 200)])
assert sfla[50] == 100 and sfla[220] == 200, "flat segments must stay flat"

# --- the curves op --------------------------------------------------------------
img = Image.open("sample.jpg").convert("RGB")
mean = lambda i: ImageStat.Stat(i).mean
m0 = mean(img)
dark = mean(apply_op(img, ("curves", {"rgb": [[0, 0], [128, 70], [255, 255]]})))
assert sum(dark) < sum(m0), "pulling the curve down should darken"
ronly = mean(apply_op(img, ("curves", {"r": [[0, 40], [255, 255]]})))
assert ronly[0] > m0[0] and abs(ronly[1] - m0[1]) < 0.01 and abs(ronly[2] - m0[2]) < 0.01

# --- sidecar round trip / hostile input ------------------------------------------
op = ["curves", {"rgb": [[0, 0], [100, 150], [255, 255]], "r": [[0, 30], [255, 255]]}]
back = clean_ops(json.loads(json.dumps([op])))
assert back == [("curves", {"rgb": [[0, 0], [100, 150], [255, 255]],
                            "r": [[0, 30], [255, 255]]})], back
assert clean_ops([["curves", {"q": [[0, 0], [255, 255]]}]]) == []      # bad channel
assert clean_ops([["curves", "junk"]]) == []                           # bad arg
assert clean_ops([["curves", {"rgb": [[0, 0], [999, -5]]}]]) == \
    [("curves", {"rgb": [[0, 0], [255, 0]]})]                          # clamped
print("PASS: curve math + op + serialization")

# --- panel interaction -------------------------------------------------------------
async def run():
    import shutil
    shutil.copy("sample.jpg", "zz_nav_tmp.jpg")   # a sibling so [ / ] navigation works
    app = TermShop("sample.jpg")
    async with app.run_test(size=(110, 32)) as pilot:
        panel = app.query_one(CurvePanel)
        assert not panel.display
        await pilot.press("k")
        assert panel.display and app.focused is panel

        # move the black point up -> live pending preview, nothing pushed
        await pilot.press("up", "up")
        assert app._pending and app._pending[0][0] == "curves"
        assert app.ops == []
        assert panel.pts["rgb"][0] == (0, 10)

        # add a point, move it, switch channel and tweak red too
        await pilot.press("a", "down")
        assert len(panel.pts["rgb"]) == 3
        await pilot.press("c")
        assert panel.ch == "r"
        await pilot.press("tab", "down")            # pull red endpoint down
        assert panel.pts["r"][1] == (255, 250)

        # apply -> one op with both channels, pending cleared, panel closed
        await pilot.press("enter")
        assert not panel.display and app._pending == []
        assert app.ops[-1][0] == "curves" and set(app.ops[-1][1]) == {"rgb", "r"}
        assert app.focused is app.view

        # sidecar has it; reload restores and renders
        data = json.loads(_P(".termshop.json").read_text())
        assert "curves" in str(data)
        app2 = TermShop("sample.jpg")
        assert app2.ops[0][0] == "curves"

        # escape cancels cleanly
        await pilot.press("k", "up", "up")
        assert app._pending
        await pilot.press("escape")
        assert app._pending == [] and not panel.display
        assert app.ops[-1][0] == "curves" and len(app.ops) == 1

        # mouse: click empty area adds + drags a point
        await pilot.press("k")
        n0 = len(panel.pts["rgb"])
        panel.plot_press(128, 200)
        assert len(panel.pts["rgb"]) == n0 + 1
        panel.plot_drag(140, 220)
        moved = panel.pts["rgb"][panel.sel]
        assert moved == (140, 220), moved
        # grab near an existing point instead of adding
        panel.plot_press(0, 5)
        assert panel.sel == 0 and len(panel.pts["rgb"]) == n0 + 1
        await pilot.press("escape")

        # switching photos cancels an open panel
        await pilot.press("k", "up")
        assert app._pending
        await pilot.press("right_square_bracket")
        assert app._pending == [] and not panel.display
    _P(".termshop.json").unlink(missing_ok=True)
    _P("zz_nav_tmp.jpg").unlink(missing_ok=True)
    print("PASS: curve panel interaction")

asyncio.run(run())
