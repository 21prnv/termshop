"""Test multi-image browsing with per-image edit history."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
import asyncio
import shutil
import tempfile
from pathlib import Path
from termshop import TermShop, list_images

async def run():
    tmp = Path(tempfile.mkdtemp(prefix="termshop_"))
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        shutil.copy("sample.jpg", tmp / name)
    (tmp / "notes.txt").write_text("not an image")
    (tmp / ".hidden.jpg").write_bytes((tmp / "a.jpg").read_bytes())

    assert [p.name for p in list_images(tmp)] == ["a.jpg", "b.jpg", "c.jpg"]

    app = TermShop(tmp)  # directory -> starts at first image
    async with app.run_test(size=(110, 32)) as pilot:
        assert app.path.name == "a.jpg" and app.file_idx == 0
        await pilot.press("B", "B")                       # edit a.jpg
        assert len(app.ops) == 2

        await pilot.press("right_square_bracket")         # -> b.jpg
        assert app.path.name == "b.jpg" and app.ops == []
        await pilot.press("g")                            # edit b.jpg

        await pilot.press("right_square_bracket")         # -> c.jpg
        await pilot.press("right_square_bracket")         # wraps -> a.jpg
        assert app.path.name == "a.jpg"
        assert [op[0] for op in app.ops] == ["brightness", "brightness"], "a.jpg history lost"

        await pilot.press("left_square_bracket")          # back -> c.jpg (wrap)
        assert app.path.name == "c.jpg" and app.ops == []

        # browse modal: starred entries for edited files, select b.jpg
        await pilot.press("o")
        from termshop import FileListScreen
        scr = app.screen
        assert isinstance(scr, FileListScreen)
        from textual.widgets import OptionList
        ol = scr.query_one(OptionList)
        prompts = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
        assert prompts[0].startswith("* ") and prompts[1].startswith("* "), prompts
        assert prompts[2].startswith("  "), prompts
        ol.highlighted = 1
        await pilot.press("enter")
        await pilot.pause()
        assert app.path.name == "b.jpg" and [op[0] for op in app.ops] == ["grayscale"]

        # single-file open still browses siblings
    app2 = TermShop(tmp / "b.jpg")
    assert app2.file_idx == 1 and len(app2.files) == 3
    shutil.rmtree(tmp)
    print("PASS: file browser + per-image history")

asyncio.run(run())
