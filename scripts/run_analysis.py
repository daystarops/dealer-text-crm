"""Compatibility wrapper for isolated, schema-driven Codex analysis."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main()->int:
    return subprocess.call([sys.executable,str(ROOT/"scripts"/"analysis_pipeline.py"),"--batch",str(ROOT/"private"/"latest-batch.json")],cwd=ROOT)


if __name__=="__main__":raise SystemExit(main())
