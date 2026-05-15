"""logos-diagnose — one-shot health check across every auth layer."""

from __future__ import annotations

import asyncio
import json
import sys

from logos.auth.diagnose import run_diagnose


def main() -> int:
    try:
        result = asyncio.run(run_diagnose())
    except Exception as e:
        print(f"Diagnose failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    # Exit code reflects live auth state — useful in CI / shell pipelines.
    return 0 if result.get("live_check", {}).get("authenticated") else 2


if __name__ == "__main__":
    sys.exit(main())
