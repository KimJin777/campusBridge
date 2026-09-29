"""Evaluation entry point.

The executable evaluator is implemented after the shared answer contracts and
development dataset schema are available. Keeping the entry point stable lets
CI and documentation use ``uv run python -m eval.run_eval`` from day one.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a campusBridge evaluation set")
    parser.add_argument("--set", dest="dataset", choices=("dev", "oos", "final"), required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-cache", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise SystemExit(
        f"evaluation runner is not wired yet (set={args.dataset!r}); "
        "implement after shared answer contracts land"
    )


if __name__ == "__main__":
    main()

