"""Generate a single-sheet workbook with more than 10k rows (exercises COPY)."""

from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook

ROWS = 12_000


def generate(path: str | Path, rows: int = ROWS) -> Path:
    """Write ``rows`` numbered rows to ``path``."""
    wb = Workbook()
    wb.remove(wb.worksheets[0])

    ws = wb.create_sheet("Big")
    ws.append(["id", "value", "label"])
    for i in range(1, rows + 1):
        ws.append([i, i * 0.5, f"row {i}"])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


if __name__ == "__main__":
    print(generate(sys.argv[1] if len(sys.argv) > 1 else "large_sheet.xlsx"))
