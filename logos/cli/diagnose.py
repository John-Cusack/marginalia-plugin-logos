"""logos-diagnose — one-shot health check across every auth layer.

Prints JSON: the session file, the parsed cookie jar (names and value lengths,
never values), the in-memory cache, the browser profile, and a live check
against the Logos API. Exit status is 0 when that live check authenticates,
2 when it does not, 1 when the diagnostic itself fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from logos.auth.diagnose import run_diagnose


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logos-diagnose",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args(argv)
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
