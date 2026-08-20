"""Tests for straightening, export options, and clipboard plumbing."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
from pathlib import Path as _P; _P(".termshop.json").unlink(missing_ok=True)
import asyncio
import json
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path
from PIL import Image
from termshop import (TermShop, ExportScreen, _max_rect, apply_op, op_size,
                      clipboard_copy_png, clipboard_paste_png)

# --- straighten math -----------------------------------------------------------
assert _max_rect(640, 480, 0) == (640, 480)
assert _max_rect(640, 480, 90) == (480, 640)
w5, h5 = _max_rect(640, 480, 5)
assert w5 < 640 and h5 < 480 and w5 > 560, (w5, h5)

img = Image.open("sample.jpg").convert("RGB")
out = apply_op(img, ("angle", 5.0))
assert out.size == op_size(img.size, ("angle", 5.0)), "op_size must match apply"
for cx, cy in ((1, 1), (out.width - 2, 1), (1, out.height - 2), (out.width - 2, out.height - 2)):
    assert sum(out.getpixel((cx, cy))) > 30, f"black wedge survived at {(cx, cy)}"
print("PASS: straighten math + auto-crop")

# --- clipboard plumbing (non-destructive: never writes the user's clipboard) -----
assert clipboard_copy_png(Path("/nonexistent/x.png")) is False
data, err = clipboard_paste_png()
assert (data is None) != (err is None) or data, (data, err)  # one of the two, no crash
print("PASS: clipboard plumbing")

# --- app flow --------------------------------------------------------------------
async def run():
    app = TermShop("sample.jpg")
    async with app.run_test(size=(110, 32)) as pilot:
        # tilt nudges merge into one op; opposite nudges cancel to nothing
        await pilot.press("full_stop", "full_stop", "greater_than_sign")
        assert app.ops == [("angle", 1.1)], app.ops
        await pilot.press("comma", "comma", "less_than_sign")
        assert app.ops == [], app.ops
        await pilot.press("full_stop")
        assert app.full_size()[0] < 640
        await pilot.press("z")
        assert app.ops == []

        # export modal with max side + quality
        await pilot.press("full_stop", "W")
        assert isinstance(app.screen, ExportScreen)
        from textual.widgets import Input
        app.screen.query_one("#exp-path", Input).value = "export_test.png"
        app.screen.query_one("#exp-size", Input).value = "200"
        await pilot.press("enter")
        await pilot.pause()
        exp = Image.open("export_test.png")
        assert max(exp.size) == 200, exp.size
        Path("export_test.png").unlink()

        # paste action never crashes regardless of clipboard state/tooling
        before = list(app.files)
        app.action_paste()
        await pilot.pause()
        for f in app.files:
            if f not in before:
                f.unlink(missing_ok=True)  # clean up if an image really was pasted
    _P(".termshop.json").unlink(missing_ok=True)
    print("PASS: tilt merge + export modal + paste safety")

asyncio.run(run())

# --- batch --max-side ---------------------------------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="termshop_"))
shutil.copy("sample.jpg", tmp / "a.jpg")
(tmp / "ops.json").write_text(json.dumps([["angle", 3.0]]))
r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent.parent / "termshop.py"), "--apply", str(tmp / "ops.json"),
                    str(tmp / "a.jpg"), "--max-side", "150"], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
assert max(Image.open(tmp / "a_edited.jpg").size) == 150
shutil.rmtree(tmp)
print("PASS: batch --max-side")
