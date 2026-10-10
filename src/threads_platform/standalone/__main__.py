"""Help-only CLI entry point for the SPLIT-01 bootstrap."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    """Create the inert CLI parser; no product capabilities are registered yet."""
    return argparse.ArgumentParser(
        prog="threads-local",
        description="Standalone product bootstrap. No Threads operations are available yet.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse help only and exit without performing external or local operations."""
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
