"""Calibrate the NordicCart demand model (VS Code: Run and Debug configurations, or the terminal).

Examples, from the repository folder:
    python python/run_demand_calibration.py                  # full profile, demand seeds 1 to 5
    python python/run_demand_calibration.py --seeds 20261117 # another demand seed
No database: the full profile is built in memory. Writes output/demand/<profile>/calibration.json.
See docs/test_reports/2026-11-18_phase3_step5_s2_demand_calibration_report.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nordiccart.demand.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
