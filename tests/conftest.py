"""Shared test fixtures."""

import sys
from pathlib import Path

# Add core src to path so plugin code can import research_engine
_core_src = Path(__file__).resolve().parents[2] / "MarginaliaAI" / "packages" / "core" / "src"
if _core_src.exists() and str(_core_src) not in sys.path:
    sys.path.insert(0, str(_core_src))
