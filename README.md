# HVBrowser

HVBrowser provides browser automation APIs for HentaiVerse. It builds on
`hbrowser` for the shared authenticated browser session.

The package was extracted from the historical combined `hbrowser`
distribution. Battle-domain APIs live in the separate `hvbattle` package.

## Development

Build a clean environment backed by the PyPI release of `hbrowser`:

```bash
bash scripts/rebuild-env.sh
```

For coordinated local development, overlay the clean environment manually:

```bash
uv pip install --python .venv/bin/python --reinstall --no-deps --editable \
  /Users/kuanlun_wang/Desktop/git-repo/hbrowser.clone
```

Commands that must preserve this editable overlay use `uv run --no-sync`.

## Read-only Market inspection

Market inspection and sale planning are non-mutating by default:

```python
import asyncio

from hvbrowser import HVDriver, SellItems


async def main() -> None:
    async with HVDriver(headless=True) as driver:
        snapshot = await driver.inspect_market()
        plan = await driver.marketcheck(SellItems(materials=["Low-Grade Metals"]))
        print(len(snapshot.items), plan.total_units)


asyncio.run(main())
```

The compatibility `marketcheck()` call requires `commit=True` before it can
request submission. Submission is currently blocked by a fail-closed runtime
gate until the current quote selector and pricing semantics have been confirmed
with the read-only live probe. The guarded implementation never deposits
credits and never re-lists unrelated orders.

## Guarded live smoke test

The surrounding `hentaiverse` workspace owns account selection. This smoke
probe only refuses to construct a browser unless credentials are supplied
indirectly through the environment. It stops when an active battle is detected
and contains no click, submit, repair, recovery, lottery, monster-lab, or battle
call:

```bash
uv run --no-sync python scripts/live_readonly_smoke.py \
  --inspect-market-form
```
