"""Single-file customer entrypoint for the existing AO-05A synthetic demo."""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, TextIO, cast

_Mode = Literal["scripted", "interactive", "readiness-only"]


def _write_status(output: TextIO) -> None:
    output.write("DEMO_MODE=SYNTHETIC\n")
    output.write("CHROME_LOGIN=NOT_EXECUTED\n")
    output.write("OAUTH_STATUS=NOT_CONNECTED\n")
    output.write("LONG_LIVED_TOKEN=NOT_ACQUIRED\n")
    output.write("REAL_API=DISABLED\n")
    output.write("SEND=DISABLED\n")
    output.flush()


def _write_readiness(output: TextIO) -> None:
    output.write("CAPABILITY STATUS\n")
    output.write("Synthetic AO-05A walkthrough AVAILABLE (fake API and temporary root)\n")
    output.write("Chrome account login EXISTING CLI CODE; NOT EXECUTED OR LIVE-VERIFIED\n")
    output.write("OAuth authorization/code exchange NOT IMPLEMENTED\n")
    output.write("Long-lived token acquisition/refresh NOT IMPLEMENTED\n")
    output.write("Owner/scope verification and Meta App Review NOT VERIFIED\n")
    output.write("Live account UAT NOT AUTHORIZED; sending DISABLED\n")
    output.flush()


def main(
    argv: Sequence[str] | None = None,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    error_stream: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    input_value = input_stream if input_stream is not None else sys.stdin
    output_value = output_stream if output_stream is not None else sys.stdout
    error_value = error_stream if error_stream is not None else sys.stderr
    environment_value = environment if environment is not None else os.environ

    parser = argparse.ArgumentParser(prog="customer_recruitment_demo")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--scripted", dest="mode", action="store_const", const="scripted")
    modes.add_argument("--interactive", dest="mode", action="store_const", const="interactive")
    modes.add_argument(
        "--readiness-only", dest="mode", action="store_const", const="readiness-only"
    )
    parser.set_defaults(mode="scripted")
    args = parser.parse_args(argv)
    mode = cast(_Mode, args.mode)

    _write_status(output_value)
    if mode == "readiness-only":
        _write_readiness(output_value)
        return 0

    source_demo = Path(__file__).with_name("demo_recruitment_journey.py")
    try:
        namespace = runpy.run_path(str(source_demo), run_name="_customer_demo_ao05a")
        demo_main = cast(Callable[..., int], namespace["main"])
    except Exception:  # noqa: BLE001 — do not expose local paths in customer-facing output.
        error_value.write("CUSTOMER_DEMO=STOP reason=AO05A_DEMO_UNAVAILABLE\n")
        error_value.flush()
        return 1

    return demo_main(
        [f"--{mode}"],
        input_stream=input_value,
        output_stream=output_value,
        error_stream=error_value,
        environment=environment_value,
    )


if __name__ == "__main__":
    raise SystemExit(main())
