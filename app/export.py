"""CSV export helpers.

Writes UTF-8 with BOM (utf-8-sig) so Excel on Windows opens Chinese text
correctly without a manual import step. CSV is chosen over .xlsx to avoid a
binary-format dependency; Excel opens these files directly.
"""
from __future__ import annotations

import csv
from pathlib import Path


def write_dicts_csv(rows: list[dict], path: Path, columns: list[str] | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = list(rows[0].keys()) if rows else []
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
