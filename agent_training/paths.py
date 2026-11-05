from __future__ import annotations

from pathlib import Path


def pipeline_root() -> Path:
    return Path(__file__).resolve().parent.parent


def config_root() -> Path:
    return pipeline_root() / "config"
