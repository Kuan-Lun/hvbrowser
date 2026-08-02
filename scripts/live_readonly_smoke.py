#!/usr/bin/env python3
"""Run the guarded, non-mutating HentaiVerse smoke probe."""

import argparse
import asyncio
import json
from dataclasses import asdict

from hvbrowser.live_probe import run_live_probe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inspect-market-form",
        action="store_true",
        help="Inspect one sell form and its public quote without clicking it",
    )
    args = parser.parse_args()
    result = asyncio.run(run_live_probe(inspect_market_form=args.inspect_market_form))
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
