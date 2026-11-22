"""ETL file publication retries while a reader (API, Power BI refresh) holds the target open (Windows only)."""
from __future__ import annotations

import os
import threading
import time

import pytest

from nordiccart import etl

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the sharing conflict exists only on Windows")


def _write(text):
    return lambda tmp: tmp.write_text(text, encoding="utf-8")


def test_replace_waits_for_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(etl, "REPLACE_WAIT", 0.1)
    target = tmp_path / "fact_sales_daily.csv"
    target.write_text("old", encoding="utf-8")
    reader = target.open("rb")                       # a reader holds the file; released after 0.35 s
    threading.Thread(target=lambda: (time.sleep(0.35), reader.close())).start()
    etl._atomic_write(target, _write("new"))
    assert target.read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "fact_sales_daily.csv.tmp").exists()


def test_replace_gives_up_after_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(etl, "REPLACE_WAIT", 0.01)
    monkeypatch.setattr(etl, "REPLACE_ATTEMPTS", 3)
    target = tmp_path / "orders_monthly.csv"
    target.write_text("old", encoding="utf-8")
    with target.open("rb"), pytest.raises(PermissionError):
        etl._atomic_write(target, _write("new"))
    assert target.read_text(encoding="utf-8") == "old"
