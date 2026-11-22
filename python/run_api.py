"""Start the NordicCart read-only analytics API (nordiccart.api) with uvicorn.

    python python/run_api.py [--host 127.0.0.1] [--port 8000] [--analytics-dir output/etl/analytics]

Swagger UI: http://127.0.0.1:8000/docs   ReDoc: /redoc   OpenAPI: /openapi.json
The API has no authentication, so it listens on localhost only unless --host is given.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--analytics-dir", type=Path, help="default: output/etl/analytics")
    args = p.parse_args()
    if args.analytics_dir:
        os.environ["NORDICCART_ANALYTICS_DIR"] = str(args.analytics_dir.resolve())
    import uvicorn

    uvicorn.run("nordiccart.api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
