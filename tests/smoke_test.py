"""Headless smoke test: drive the app with Textual's Pilot, verify save output."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
import asyncio
from pathlib import Path
from PIL import Image
from termshop import TermShop

from pathlib import Path as _P; _P(".termshop.json").unlink(missing_ok=True)
SAMPLE = Path("sample.jpg")
OUT = Path("sample_edited.jpg")

async def run():
    OUT.unlink(missing_ok=True)
    app = TermShop(str(SAMPLE))
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.press("r")                     # rotate cw: 640x480 -> 480x640
        await pilot.press("B", "N", "S", "g", "a") # adjustments + filters
        await pilot.press("z", "z", "y")           # undo x2, redo x1 -> net: autocontrast undone
        assert len(app.ops) == 5, app.ops
        app._push(("crop", (0.25, 0.25, 0.75, 0.75)))  # simulated mouse crop
        await pilot.pause()
        assert app.full_size() == (240, 320), app.full_size()
        app.action_save()
        await pilot.pause()
    img = Image.open(OUT)
    assert img.size == (240, 320), img.size
    print("PASS: ops =", [o[0] for o in app.ops], "| saved", OUT, img.size)

asyncio.run(run())
