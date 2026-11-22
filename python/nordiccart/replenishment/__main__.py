"""Allows `python -m nordiccart.replenishment` from the python/ folder."""

import sys

from .cli import main

sys.exit(main())
