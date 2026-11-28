"""Verify current dynamic searches and replay with both network paths forbidden."""

import sys

from src.verification import main


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        sys.exit(1)
