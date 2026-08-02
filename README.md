# HVBrowser

HVBrowser provides browser automation APIs for HentaiVerse. It builds on
`hbrowser` for the shared authenticated browser session.

The package was extracted from the historical combined `hbrowser`
distribution. Battle-domain APIs live in the separate `hvbattle` package.

## Lottery and Monster Lab APIs

Lottery inspection is read-only and returns the current ticket count, shared GP
balance, and the fixed 1,000 GP ticket price. Purchasing requires an explicit
lottery kind and positive quantity, then verifies the exact ticket increase and
GP decrease before returning:

```python
from hvbrowser import LotteryClient, LotteryKind

client = LotteryClient(driver)
snapshot = await client.inspect(LotteryKind.WEAPON)
report = await client.purchase(
    LotteryKind.WEAPON,
    25,
    expected_before=snapshot,
)
```

The optional snapshot is an optimistic precondition: if tickets, GP, lottery
kind, or price changed after planning, the client raises before touching the
purchase form.

Target counts, Weapon-versus-Armor priority, and behavior when GP is limited are
application policy and intentionally do not belong in this package. The legacy
`HVDriver.loetterycheck()` spelling remains as a compatibility wrapper.

Monster Lab exposes the two known feed-all resources explicitly:

```python
from hvbrowser import MonsterLabClient, MonsterLabFeed

client = MonsterLabClient(driver)
snapshot = await client.inspect()
food_report = await client.feed_all(MonsterLabFeed.FOOD)
drugs_report = await client.feed_all(MonsterLabFeed.DRUGS)
```

After the Monster Lab API is confirmed, an action image that is not present is
reported as unavailable and produces no mutation. The selector and
post-submission disappearance semantics still require a read-only live selector
check and a separately authorized mutation check, respectively. Missing page
structure, unknown submission outcomes, and unstable/failed post-submit
confirmation raise typed errors. Both purchase and feed-all calls change
account state; automated package tests use only offline fakes.

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
and contains no state-changing purchase, feed-all, submit, repair, recovery, or
battle call. It does use navigation clicks to reach the requested read-only
pages:

```bash
uv run --no-sync python scripts/live_readonly_smoke.py \
  --skip-market \
  --inspect-lotteries \
  --inspect-monster-lab
```

`--skip-market` lets the two new selectors be checked independently while the
Market integration is unavailable. The last two flags only inspect counts and
action availability. They never call the Lottery purchase form or
`do_feed_all`.
