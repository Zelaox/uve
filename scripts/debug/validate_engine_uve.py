#!/usr/bin/env python3
"""UVE command_check — AI Lab engine impact-gate verify scenario suite."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.diagnostics.engine_uve_validate import main

if __name__ == "__main__":
    raise SystemExit(main())
