#!/usr/bin/env python3
"""Launch Orion CLI with Chronicler-compatible Matcher patches."""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import patch_orion_matcher  # noqa: F401,E402
import patch_orion_utils  # noqa: F401,E402

from main import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
