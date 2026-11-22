"""Run the NordicCart master data seeder (VS Code: open this file and press Run, or use the terminal).

Examples, from the repository folder:
    python python/run_master_data_seed.py                  # dry run: generate, validate, CSV preview
    python python/run_master_data_seed.py --plan           # compare with the database, read-only
    python python/run_master_data_seed.py --apply          # insert missing rows (asks to confirm)
    python python/run_master_data_seed.py --profile full   # 500 products, 20 000 customers
See docs/phase3_master_data_generator_guide.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nordiccart.master_data.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
