"""Fixtures for MSG-07 security tests: the shared integration harness (itfixtures.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))

from itfixtures import *  # noqa: E402,F403
