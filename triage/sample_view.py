"""The three sample files exactly as they sit on disk, for the "Show sample data" view.

Nothing is cleaned: the Tally heading lines above the header, the Grand Total footer, the mixed date and amount
formats and the blank cells all come through, so a visitor sees what the engine is given before it runs.
"""
import csv
import io
from pathlib import Path

from .engine import SAMPLE_FILES

MAX_LINES = 100  # per file; the bundled files are under 50 lines


def sample_view(sample_dir) -> dict:
    """{"files": {kind: {"filename", "total", "width", "rows"}}}. Each row is a list of cell texts, one per line."""
    files = {}
    for kind, name in SAMPLE_FILES.items():
        text = (Path(sample_dir) / name).read_bytes().decode("utf-8-sig", errors="replace")
        lines = list(csv.reader(io.StringIO(text)))
        shown = lines[:MAX_LINES]
        files[kind] = {"filename": name, "total": len(lines), "width": max((len(r) for r in shown), default=0),
                       "rows": shown}
    return {"files": files}
