# HVBrowser

HVBrowser provides typed browser automation components for HentaiVerse. It
builds on `hbrowser` for one authenticated browser transport; reusable battle
operations remain in the separate `hvbattle` package.

## Component graph

`HentaiVerseSession` is the public composition root. `HVDriver` is deliberately
limited to browser lifecycle and authenticated transport, while every
HentaiVerse feature has a named component:

```text
HentaiVerseSession
├── browser: HVDriver
├── realm: RealmNavigator
├── maintenance_navigation: MaintenanceNavigator
├── player: PlayerClient
├── equipment: EquipmentRepairClient
├── market: MarketClient
├── lottery: LotteryClient
└── monster_lab: MonsterLabClient
```

```python
from hvbrowser import HentaiVerseSession, Realm


async with HentaiVerseSession(headless=True) as session:
    player = await session.player.inspect()
    realm = await session.realm.current()
    print(player.level, player.stamina, realm is Realm.ISEKAI)
```

The session initializes the browser, authenticates, enters Persistent, and
closes the browser on exit. An application may inject a configured but
not-yet-started transport with `HentaiVerseSession(browser=browser)`; after
injection, the session owns that transport's lifecycle.

All components share the same browser page. Call their operations sequentially;
concurrent navigation by multiple components is not supported.

## Player and equipment

Player reads and mutations are explicit. Stamina recovery returns a
`StaminaRecoveryReport`; a successful report confirms that the visible stamina
increased. Missing controls and unknown submission outcomes raise typed errors.

```python
before = await session.player.read_stamina()
report = await session.player.recover_stamina(expected_before=before)
if report.recovered:
    print(report.before, report.after)
```

Equipment repair separates inspection from mutation and scopes snapshots to a
typed `Realm`. `repair_all()` returns `NO_REPAIR_NEEDED`, `REPAIRED`, or
`MATERIALS_UNAVAILABLE`. It verifies a disabled submit control against a fresh
server page before reporting unavailable materials.

```python
snapshot = await session.equipment.inspect()
report = await session.equipment.repair_all(expected_before=snapshot)
if not report.ready:
    print(report.outcome)
```

## Lottery and Monster Lab

Lottery inspection returns the current ticket count, shared GP balance, and
fixed ticket price. Purchasing requires an explicit kind and positive quantity,
then verifies the exact ticket increase and GP decrease.

```python
from hvbrowser import LotteryKind

snapshot = await session.lottery.inspect(LotteryKind.WEAPON)
report = await session.lottery.purchase(
    LotteryKind.WEAPON,
    25,
    expected_before=snapshot,
)
```

Target counts, Weapon-versus-Armor priority, and limited-GP behavior are
application policy and do not belong in this package.

Monster Lab similarly exposes each resource as one atomic operation:

```python
from hvbrowser import MonsterLabFeed

snapshot = await session.monster_lab.inspect()
food = await session.monster_lab.feed_all(MonsterLabFeed.FOOD)
drugs = await session.monster_lab.feed_all(MonsterLabFeed.DRUGS)
```

Lottery, Monster Lab, and equipment repair share one fail-closed
`MaintenanceNavigator`. An atomic DOM snapshot classifies timed challenge,
final-completion, next-floor, and active-battle markers before unsafe Bazaar
interaction. A marker raises `MaintenanceNavigationBlockedError` with a typed
`blocker`. Marker-free missing Bazaar state permits exactly one same-realm
retry.

## Market

Market inspection and sale planning are read-only. A `MarketSaleRequest`
contains immutable item-name selections, and the resulting plan records its
realm.

```python
from hvbrowser import MarketSaleRequest

snapshot = await session.market.inspect()
plan = await session.market.plan_sales(
    MarketSaleRequest(materials=("Low-Grade Metals",)),
)
print(snapshot.realm, plan.total_units)
```

Submission requires an explicit `submit_sales(plan)` call. It remains blocked
by a fail-closed runtime gate until the current live quote and pricing semantics
have been verified. The guarded implementation never deposits credits or
re-lists unrelated orders.

## Guarded live smoke test

The surrounding application owns account selection. The smoke probe requires
credentials through environment indirection, stops on active battle markers,
and performs no purchase, feed-all, Market submission, repair, recovery, or
battle action:

```bash
uv run --no-sync python scripts/live_readonly_smoke.py \
  --skip-market \
  --inspect-lotteries \
  --inspect-monster-lab
```

Lottery and Monster Lab inspection are skipped in Isekai without switching
realms.

## Development

Build a clean environment backed by the PyPI release of `hbrowser`:

```bash
bash scripts/rebuild-env.sh
```

For coordinated local development, overlay the local checkout after rebuilding:

```bash
uv pip install --python .venv/bin/python --reinstall --no-deps --editable \
  /Users/kuanlun_wang/Desktop/git-repo/hbrowser.clone
```

Commands that must preserve the editable overlay use `uv run --no-sync`.
