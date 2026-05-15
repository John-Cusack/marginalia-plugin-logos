"""logos-login — open browser, sign in, persist cookies."""

from __future__ import annotations

import asyncio
import sys

from logos.auth.manager import refresh_auth


def main() -> int:
    try:
        jar = asyncio.run(refresh_auth())
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Login failed: {e}", file=sys.stderr)
        return 1
    print(f"Saved {len(jar.cookies)} cookies.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
