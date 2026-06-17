#!/usr/bin/env python3
"""Command-line interface for the plain-language -> Azure Terraform translator.

Usage:
    python cli.py "a linux web app with a postgres flexible server database"
    echo "a storage account with a private blob container" | python cli.py

Requires the ANTHROPIC_API_KEY environment variable to be set.
"""

from __future__ import annotations

import sys

from translator import TranslationError, translate


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        prompt = " ".join(argv[1:])
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    try:
        print(translate(prompt))
    except TranslationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
