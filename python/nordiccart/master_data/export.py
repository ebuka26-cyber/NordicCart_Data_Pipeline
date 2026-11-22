"""CSV preview of a generated dataset (one file per table) plus a summary.json.

Written on every run so the data can be inspected in Excel or VS Code before any
database is touched. UTF-8 with BOM so Excel shows å, ä, ö, ø and æ correctly.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, fields
from datetime import datetime, timezone
from pathlib import Path

from .model import MasterDataset, canonical


def export_csv(dataset: MasterDataset, directory: Path, extra_summary: dict | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in dataset.tables().items():
        path = directory / f"{name}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            if rows:
                columns = [f.name for f in fields(rows[0])]
                writer.writerow(columns)
                for row in rows:
                    values = asdict(row)
                    writer.writerow(["" if canonical(values[c]) is None else canonical(values[c]) for c in columns])
    summary = {
        "profile": dataset.profile,
        "seed": dataset.seed,
        "fingerprint_sha256": dataset.fingerprint(),
        "rows": dataset.row_counts(),
        "written_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if extra_summary:
        summary.update(extra_summary)
    (directory / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return directory
