"""Test sidecar persistence and the sidebar histogram."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from termshop import TermShop, load_sidecar, spark_hist, SIDECAR_NAME

# --- spark_hist math ----------------------------------------------------------
from pathlib import Path as _P; _P(".termshop.json").unlink(missing_ok=True)
flat = spark_hist([100] * 256)
assert len(flat) == 30 and len(set(flat)) == 1 == len(set(flat)), flat
peaked = spark_hist([0] * 128 + [1000] + [0] * 127)
assert peaked.count("█") == 1 and peaked.strip("█ ") == "", repr(peaked)
assert spark_hist([0] * 256) == " " * 30  # all-zero doesn't divide by zero
print("PASS: spark_hist")

# --- sidecar round-trip ---------------------------------------------------------
async def run():
    tmp = Path(tempfile.mkdtemp(prefix="termshop_"))
    for name in ("a.jpg", "b.jpg"):
        shutil.copy("sample.jpg", tmp / name)
    sidecar = tmp / SIDECAR_NAME

    # session 1: edit both files
    app = TermShop(tmp)
    async with app.run_test(size=(110, 32)) as pilot:
        await pilot.press("B", "r")                       # a.jpg
        await pilot.press("right_square_bracket", "g")    # b.jpg
    data = json.loads(sidecar.read_text())
    assert data["edits"]["a.jpg"] == [["brightness", 1.08], ["rotate_cw", None]], data
    assert data["edits"]["b.jpg"] == [["grayscale", None]]

    # session 2: edits restored, undo works on them, clearing removes entries
    app2 = TermShop(tmp / "a.jpg")
    async with app2.run_test(size=(110, 32)) as pilot:
        assert [op[0] for op in app2.ops] == ["brightness", "rotate_cw"]
        assert app2.full_size() == (480, 640)             # rotate persisted
        await pilot.press("z", "z")                       # undo both -> a.jpg clean
    data = json.loads(sidecar.read_text())
    assert "a.jpg" not in data["edits"] and "b.jpg" in data["edits"], data

    # clearing the last edit deletes the sidecar entirely
    app3 = TermShop(tmp / "b.jpg")
    async with app3.run_test(size=(110, 32)) as pilot:
        await pilot.press("z")
    assert not sidecar.exists(), "sidecar should be removed when no edits remain"

    # corrupt / hostile sidecars are ignored
    sidecar.write_text("{ not json")
    assert load_sidecar(sidecar) == {}
    sidecar.write_text(json.dumps({"edits": {"a.jpg": [["rm -rf", 1], ["brightness", 1.1], "junk"]}}))
    assert load_sidecar(sidecar) == {"a.jpg": [("brightness", 1.1)]}
    app4 = TermShop(tmp)  # boots fine with hostile sidecar
    assert app4.sessions

    shutil.rmtree(tmp)
    print("PASS: sidecar persistence")

    # histogram appears in sidebar
    app5 = TermShop("sample.jpg")
    async with app5.run_test(size=(110, 32)) as pilot:
        text = str(app5.query_one("#side").render())
        assert "history" in text
        import termshop
        assert any(ch in text for ch in termshop.SPARK[1:]), "no sparkline in sidebar"
    print("PASS: histogram in sidebar")

asyncio.run(run())
