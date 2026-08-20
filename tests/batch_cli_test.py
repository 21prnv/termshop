"""Test the batch CLI: --commit, --apply, --clear, collision guard."""
import os as _os; _os.chdir(_os.path.dirname(_os.path.abspath(__file__)))  # tests run from tests/
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PY = sys.executable
TERMSHOP = str(Path(__file__).resolve().parent.parent / "termshop.py")
ROT_CROP = [["rotate_cw", None], ["crop", [0.25, 0.25, 0.75, 0.75]]]

def run(*argv):
    return subprocess.run([PY, TERMSHOP, *argv], capture_output=True, text=True)

tmp = Path(tempfile.mkdtemp(prefix="termshop_"))
for name in ("a.jpg", "b.jpg", "c.jpg"):
    shutil.copy("sample.jpg", tmp / name)
sidecar = tmp / ".termshop.json"
sidecar.write_text(json.dumps({"version": 1, "edits": {"a.jpg": ROT_CROP}}))

# --commit a directory: only a.jpg has edits
r = run("--commit", str(tmp))
assert r.returncode == 0, r.stderr
assert (tmp / "a_edited.jpg").exists() and not (tmp / "b_edited.jpg").exists()
from PIL import Image
assert Image.open(tmp / "a_edited.jpg").size == (240, 320)  # 640x480 -> rot -> crop
assert "done: 1 file(s)" in r.stdout, r.stdout
assert sidecar.exists(), "--commit without --clear must keep the sidecar"

# --commit --clear to an outdir: same name, entry removed, sidecar gone (was last)
r = run("--commit", str(tmp), "--clear", "-o", str(tmp / "out"))
assert r.returncode == 0, r.stderr
assert (tmp / "out" / "a.jpg").exists()
assert not sidecar.exists(), "--clear should remove the exhausted sidecar"

# --commit with nothing to do fails
r = run("--commit", str(tmp))
assert r.returncode != 0 and "nothing to export" in (r.stdout + r.stderr)

# --apply from an edited image's sidecar to other files
sidecar.write_text(json.dumps({"version": 1, "edits": {"a.jpg": ROT_CROP}}))
r = run("--apply", str(tmp / "a.jpg"), str(tmp / "b.jpg"), str(tmp / "c.jpg"))
assert r.returncode == 0, r.stderr
assert Image.open(tmp / "b_edited.jpg").size == (240, 320)
assert Image.open(tmp / "c_edited.jpg").size == (240, 320)

# --apply a raw ops json to a directory target
ops_json = tmp / "ops.json"
ops_json.write_text(json.dumps([["grayscale", None]]))
outdir = tmp / "gray"
r = run("--apply", str(ops_json), str(tmp), "-o", str(outdir))
assert r.returncode == 0, r.stderr
n = len(list(outdir.glob("*.jpg")))
assert n >= 3, f"expected >=3 grayscale outputs, got {n}"
g = Image.open(outdir / "b.jpg").convert("RGB")
px = g.getpixel((10, 10))
assert px[0] == px[1] == px[2], "grayscale not applied"

# collision guard: suffix '' without outdir would overwrite input
r = run("--apply", str(ops_json), str(tmp / "b.jpg"), "--suffix", "")
assert r.returncode != 0 and "overwrite" in r.stdout, r.stdout

# batch flags without batch mode are rejected
r = run(str(tmp / "a.jpg"), "-o", "x")
assert r.returncode != 0 and "only makes sense" in r.stderr

# --apply with a multi-entry sidecar json is ambiguous
sidecar.write_text(json.dumps({"version": 1, "edits": {"a.jpg": ROT_CROP, "b.jpg": ROT_CROP}}))
r = run("--apply", str(sidecar), str(tmp / "c.jpg"))
assert r.returncode != 0 and "2 pipelines" in (r.stdout + r.stderr)

shutil.rmtree(tmp)
print("PASS: batch CLI")
