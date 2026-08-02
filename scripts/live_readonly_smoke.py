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
        "--skip-market",
        action="store_true",
        help="Skip Market inspection so other selectors can be checked alone",
    )
    parser.add_argument(
        "--inspect-market-form",
        action="store_true",
        help="Inspect one sell form and its public quote without clicking it",
    )
    parser.add_argument(
        "--inspect-lotteries",
        action="store_true",
        help="Inspect Weapon and Armor Lottery counts without purchasing",
    )
    parser.add_argument(
        "--inspect-monster-lab",
        action="store_true",
        help="Inspect both Monster Lab feed-all selectors without feeding",
    )
    args = parser.parse_args()
    if args.skip_market and args.inspect_market_form:
        parser.error("--inspect-market-form cannot be used with --skip-market")
    result = asyncio.run(
        run_live_probe(
            inspect_market=not args.skip_market,
            inspect_market_form=args.inspect_market_form,
            inspect_lotteries=args.inspect_lotteries,
            inspect_monster_lab=args.inspect_monster_lab,
        )
    )
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
