# Wraeclast Ledger

Wraeclast Ledger is a local-first Path of Exile 1 softcore-trade market
research tool. It downloads current economy data, retains the source responses
and normalized price history in SQLite, aligns the same item by league day
across completed leagues, compares today's price with a recency-weighted
future historical level, blends it with the current-league price curve, and
produces gross 3, 7, and 14-day gain forecasts. The selected horizon determines
the global order shown across paginated results. The archive is intentionally
broad, while excluded small-consumable categories, sub-1-chaos markets, and
persistent completed-league decliners remain outside the displayed investment
universe. Base types other than Simplex Amulet and Focused Amulet, unique-item
categories, Valdo maps, and non-Awakened skill gems are also archived but
omitted from rankings. The unique-item exceptions are
one- and three-passive Voices, Sublime Vision, The Adorned, and Watcher's Eye;
Awakened gems and the separately categorized Forbidden Jewel market also
remain visible. The latter three use aggregate poe.ninja markets and are
labelled roll-unresolved rather than presented as prices for sought rolls.
The Voices trade links preserve the exact one-/three-passive count. The
Adorned trade link targets 90% or greater effect, but its displayed price and
forecast remain the clearly labelled aggregate poe.ninja family series.

The application does not place trades. Recommendations are probabilistic
research signals, not guarantees of profit.

## Quick start

Wraeclast Ledger requires Python 3.11 or newer and has no third-party runtime
dependencies.

On Windows PowerShell:

```powershell
.\run.ps1
```

From Command Prompt:

```bat
run.cmd
```

Or launch it directly:

```powershell
python -m poe_advisor serve --open
```

The local server listens on `http://127.0.0.1:8787` by default. The launchers
look for a compatible interpreter through the Windows `py` launcher, then
`python`, `python3`, and finally the bundled Codex workspace runtime when it is
available.

The server deliberately accepts only `127.0.0.1` or `localhost` bindings. Its
mutation endpoints are designed for a trusted local process, not a shared
network service.

If live sources are unreachable on the first run, the application can load a
clearly labelled offline demo fixture. Demo values are never reported as live
prices.

After the first live market sync, open the gear menu and click **Build /
continue past-league archive**. The first pass imports the four selected
broad-league ZIP dumps listed by poe.ninja; later passes use saved dump
fingerprints to skip every unchanged league. Forecasts use exact item/day
observations from those softcore trade leagues: Mirage, Keepers (also
reported upstream as Keepers of the Flame), Mercenaries, and Settlers.

## GitHub Pages and automatic daily updates

The same dashboard can run as a read-only GitHub Pages site without exposing
the local API or a raw copy of the local SQLite database. The included
`.github/workflows/daily-pages.yml` workflow:

1. Restores the most recent sanitized, compressed market database from the
   `market-archive` GitHub Release.
2. Runs the full unattended refresh: poe.ninja current overviews, exact
   dated detail histories for every currently ranked identity (up to 2,000),
   optional build composition, any not-yet-imported broad-league official
   dumps, and the recommendation model.
3. Exports a lightweight all-item search/filter index, small content-hashed
   3/7/14-day ranking pages, and lazily loaded price-curve shards. The hosted
   page never downloads or parses one giant all-item recommendation catalog.
4. Creates an integrity-checked, public-market-only SQLite backup, preserves
   the safe poe.ninja dump checkpoints so immutable ZIPs are imported only
   once, and rotates the prior good backup as
   `poe_market_compact_history.previous.sqlite3.gz`. Raw HTTP
   payloads, local diagnostics, and settings outside a narrow crawl allowlist
   are removed before compression.
5. Deploys only the static dashboard data to GitHub Pages.

The scheduled run starts daily at 09:23 in `America/Los_Angeles`. It can also
be started manually from the repository's **Actions** tab. On GitHub Pages,
the dashboard's primary update button opens that workflow; it never embeds a
GitHub token in browser code. A small successful-run heartbeat is committed so
public-repository schedules do not become dormant after 60 days. A failed
refresh does not replace the previous Pages deployment or archive snapshot.
The scheduled and normal manual runs leave the optional GGG hourly exchange
audit backfill disabled; a positive recovery window must be selected explicitly
when dispatching the workflow.

GitHub Pages must use **GitHub Actions** as its publishing source. The
repository workflow needs `contents: write`, `pages: write`, and
`id-token: write`; those permissions are scoped in the workflow file. Set an
optional `POE_OAUTH_TOKEN` repository secret only if an official endpoint
later requires it. The public data sources used by the normal refresh do not
require that secret.

To create the static artifact locally:

```powershell
python -m poe_advisor --db data/poe_advisor.sqlite3 export-pages `
  --output pages-dist --repository OWNER/REPOSITORY
```

To create the sanitized durable release asset safely even while the local
server is open:

```powershell
python -m poe_advisor --db data/poe_advisor.sqlite3 archive-snapshot `
  --output build/archive/poe_market_compact_history.sqlite3.gz `
  --public-market-only
```

Generated Pages files and database backups are ignored by Git. The full
database is never part of the Pages artifact or normal repository history.
For a public repository, its GitHub Release database assets are also public.
The workflow therefore uploads only normalized public market/model data. It
does not upload stored raw API response bodies, arbitrary local settings,
browser preferences, diagnostic error text, filesystem paths, or credentials.

## What is stored locally

The SQLite database contains:

- League metadata and whether the active dataset is live or demo.
- Successful current/detail response bodies, gzip-compressed and
  content-hashed.
- Divine-relative normalized price observations.
- Exact poe.ninja source item IDs and variant metadata from the current
  overview catalog.
- Exact dated current-league poe.ninja detail histories for ranked items,
  including the raw item and same-day Divine/Chaos responses used for
  normalization.
- Current overview observations may be retained when a user syncs repeatedly,
  but model curves select only the newest exact poe.ninja observation in each
  UTC league-day bucket.
- Completed-league daily prices imported from poe.ninja's official ZIP dumps,
  plus the ZIP fingerprint/checkpoint, direct Chaos quote, confidence, and
  source metadata retained for fast queries.
- Optional official GGG hourly exchange responses when an audit backfill is
  explicitly requested. These rows are never forecast or ranking inputs.
- Prices for excluded low-end markets, including essences, fossils, oils,
  resonators, scarabs, Delirium Orbs, artifacts, and incubators. Keeping these
  observations does not place those categories in the forecast ranking.
- Exact-match Standard prices used only as a separately labelled long-term
  convergence reference.
- The official GGG passive-tree export used to map exact Forbidden Jewel
  passives to their owning ascendancy and base class.
- Sampled ascendancy shares from the official public ladders, including the
  raw ladder pages used to calculate them.
- Per-source ETag and last-success state.
- Sync-run outcomes and warnings.
- Generated recommendation runs and public settings.

The default database is under the local `data/` directory. Set
`POE_ADVISOR_DB` to an absolute or relative path to put it elsewhere. Back up
that database to preserve the history accumulated by daily syncs.

Raw snapshots are append-only by content hash. Repeated `304 Not Modified`
responses do not duplicate them. Completed-league ZIPs are streamed through a
temporary file; their normalized rows, SHA-256 fingerprint, and successful
import checkpoint remain in SQLite, so an unchanged dump is not downloaded
again.

## Sync and backfill behavior

The **Sync market & run model** button calls `POST /api/sync`. A normal sync:

1. Discovers the current PoE 1 softcore trade league from poe.ninja.
2. Uses conditional requests to fetch the configured full PoE 1 poe.ninja
   universe by default: 18 exchange categories and 25 item-overview
   categories. This includes currencies, fragments, divination cards,
   crafting consumables, uniques, gems, maps, bases, beasts, and the other
   currently documented modalities. Collection scope is broader than
   recommendation scope, so excluded consumables still accumulate local
   history.
3. Preserves poe.ninja's exact source IDs and visible variant fields; an item
   without a unique identity is not joined to historical data.
4. Stores GGG's official passive-tree export as metadata for exact Forbidden
   Jewel identities; ambiguous or unknown passives remain unmapped.
5. Stores each raw response before writing normalized price points and leaves
   existing local data intact when one source or category fails.
6. Archives poe.ninja's indexed-build class distribution and its nearest
   available past-league time-machine snapshots. The much smaller official
   experience-ladder sample is used only when poe.ninja is unavailable.
7. Runs a preliminary complete ranking and requests every in-scope exact
   identity's dated poe.ninja detail history (up to the 2,000-item safety cap).
   Prices are normalized only when that item day has an exact same-day
   poe.ninja Divine/Chaos observation; missing trades and missing anchors remain
   gaps. A compact coverage checkpoint survives hosted-archive cleanup, verifies
   that every normalized day still has a stored price row, and rechecks each
   exact curve at least weekly so interrupted or late provider data can heal.
8. Regenerates and stores the final ranking after the curve archive is updated.

Hourly exchange collection is disabled by default. A positive optional JSON
field `backfill_hours` explicitly advances the official GGG Currency Exchange
audit archive:

```json
{"backfill_hours": 24}
```

The official feed is historical, mixes all leagues, and does not expose the
current hour. Wraeclast Ledger filters the selected league locally and saves a
resume cursor. Backfill is intentionally bounded; it does not crawl the entire
multi-year archive in one click. This is a separate research archive and never
replaces a poe.ninja current or historical price in the forecast.

Recent official hourly payloads can be roughly 1–2 MB each before local gzip
compression. A 24-hour backfill may therefore download tens of megabytes and
take several minutes depending on rate limits and connection speed. The first
poe.ninja sync is normally much smaller. Repeated syncs are cheaper because
ETag and immutable-snapshot caching are used.

The separate completed-league button calls `POST /api/seasonal/backfill`. Each
pass checks the official dump catalog at
[poe.ninja/poe1/data](https://poe.ninja/poe1/data), then imports each new or
changed ZIP for the four broadly covered scoring leagues as one complete unit.
Every usable row in those selected ZIPs is retained; item rows that do not yet
match a current exact identity remain archive-only and cannot be scored.
Currency rows must be quoted
directly against Chaos, and every Divine conversion uses the direct
same-date Divine/Chaos pair from that same ZIP. Unmatched IDs, identity
mismatches, and missing anchors remain explicit gaps.

The ZIP fingerprint and success marker are committed with the normalized rows
in SQLite. Repeated local passes and scheduled GitHub Actions runs therefore
skip already imported immutable dumps while retaining the checkpoint in the
durable sanitized archive.

Local imports keep the full verbose `seasonal_prices` archive by default,
including archive-only identities and per-row diagnostics. GitHub Actions sets
`POE_ADVISOR_COMPACT_HISTORY=1`; in that mode each eligible official daily
value is streamed through an atomic staging table into integer-keyed
`WITHOUT ROWID` storage. League, item, and source-item strings are stored once
in small dictionaries, while the exact league day, observation timestamp,
Chaos value, Divine value, and confidence remain unchanged. Dump markers keep
both exact raw/normalized source counts and the smaller exact stored-row count.
This is what lets a first bootstrap and later archive snapshots fit a standard
14 GB GitHub runner without weakening the poe.ninja-only provenance rule.

An existing full local archive can be converted without a download and without
deleting its original rows:

```powershell
python -m poe_advisor --db data/poe_advisor.sqlite3 compact-history
```

The conversion streams one league at a time, promotes it atomically, and skips
already converted leagues on a rerun. Use repeatable `--league LEAGUE` options
to limit the conversion, or `--force` to rebuild compact rows. Seasonal curve,
lifecycle, same-day, forward-return, and status queries read the compact
representation transparently whenever it is present. A public-market snapshot
then removes the redundant verbose official rows only from its isolated copy;
the full local source database is never pruned.

## Historical-data limitations

poe.ninja's current overview APIs supply the latest market catalog and prices;
their relative sparklines are never expanded into invented dates. Exact dated
current-league curves come from poe.ninja's exchange-detail and stash-item
history responses. Completed-league curves come only from poe.ninja's official
ZIP dumps.

Both current- and completed-league model curves are daily. For current-league
data, the newest exact poe.ninja observation in each UTC league-day bucket is
used; completed-league dumps already provide exact daily buckets. Intraday rows
can remain in the local audit archive without increasing model granularity.

The official GGG Currency Exchange feed provides genuine hourly historical
digests for fungible markets. Old hours may eventually be removed upstream,
which is why downloaded snapshots are retained locally. The golden-source
allowlist excludes these rows from scored curves.

The completed-league importer is deliberately exact. Item CSV rows join by
poe.ninja source ID and then pass a visible-identity check; skill-gem level,
quality, and corruption and other variant fields cannot be mixed. Currency
prices and the same-day Divine anchor must be direct Chaos pairs from the dump.
There is no adjacent-day interpolation or cross-provider fallback. Missing
rows mean “no usable evidence,” never a zero price.

Legacy poe.watch responses and derived rows may remain in an upgraded local
database as an auditable quarantine, and optional poe.watch league/catalog
metadata may still be archived. Production price queries fail closed to the
poe.ninja source allowlist: poe.watch rows can never supply a current price,
historical target, Standard reference, chart curve, or ranking score.

If an item lacks an exact future-day price in the broadly covered leagues, the
affected horizon is shown as `—` with “no exact broad-league future-day
price.” Missing evidence is never converted to a 0% return.

poe.ninja build distributions and official ladder samples remain in the local
archive as metadata. For Forbidden Jewels only, a valid current-versus-past
ascendancy-share signal scales the historical target; the unadjusted
poe.ninja price observations remain visible for audit.

## Forecast model

### Investment universe

Essence, Fossil, Oil, Resonator, Scarab, Delirium Orb, Artifact, and Incubator
categories remain outside the displayed ranking, as requested. Any current
market below 1 Chaos is also omitted; this is a minimum unit-price floor, not an
upper price or budget cap. The model additionally removes an exact item when
its weekly median Divine-relative curve robustly declines across at least two
broadly covered past leagues with at least 65% of the available recency weight.
BaseType markets are omitted except for Simplex Amulet and Focused Amulet.
All excluded prices are still archived locally for research and fast queries.

The automatic lifecycle rule requires at least 12 usable weekly buckets over a
70-day span in each contributing league. A league must show a Theil-Sen trend
of at most -2% per week, at least 70% negative pairwise slopes, and a late/early
price ratio no greater than 0.80. These conditions resist daily noise and old
mapping anomalies while detecting currencies such as Chaos Orb that reliably
lose Divine-relative value as a league matures.

### Broad-league evidence

Forecast calculations use only the four broadly covered leagues:

- Mirage
- Keepers (including the upstream name `Keepers of the Flame`)
- Mercenaries
- Settlers

Legacy archives may still contain Affliction and Necropolis rows, but they do
not contribute to forecasts.
For each item and horizon, the model reads the exact historical price at the
current league day plus 3, 7, or 14 days. Only poe.ninja observations graded
Medium or High (normalized confidence at least `0.5`) may enter a forecast,
the same-day comparison, or the structural-decline classifier. The displayed
weighted historical curve includes every positive exact poe.ninja row,
including Low-confidence context, while exposing the number of forecast-grade
contributors separately. Low rows never set a forecast target. The qualifying
recency-weighted future price is compared directly with today's current-league
price; a historical entry price is not required.
The qualifying future-day observation count is shown per horizon.
Missing observations stay missing; the dashboard displays `—` and “no exact
broad-league future-day price,” never 0%.

The available broad-league future prices are recency weighted. Newer leagues
receive more influence than older leagues, and the available weights are
renormalized to 100% rather than treating a missing league as a zero-price
observation.

### Gross expected gain

For each horizon:

1. Calculate the recency-weighted historical future price level from exact
   Medium/High-confidence broad-league future-day observations.
2. Estimate a robust log-price trend from the current-league curve and cap its
   projected move so a brief spike or crash cannot dominate.
3. When the current curve is usable, blend 70% historical future level and 30%
   current-curve projection in log price space. Otherwise use the historical
   target by itself.
4. Convert the blended future price to a gross percentage gain from the latest
   current price.

In compact form:

`log(expected price) = 0.70 × log(historical target) + 0.30 × log(capped current-curve target)`

`expected gain = expected price / current price - 1`

After the investment-universe exclusions above, the output does not deduct
transaction friction or adjust for liquidity, confidence within the qualifying
Medium/High set, falling-knife behavior, disagreement penalties, Standard
prices, or build demand. A negative forecast remains visible and ranks below a
higher forecast. An item with no qualifying target remains in the paginated
universe with a missing forecast.

The chosen 3, 7, or 14-day hold window is the sort horizon. Every row still
shows all three estimates, their historical targets, current-curve components,
and broad-league sample counts in the detail view. Budget, item price, and
portfolio allocation do not affect the order.

The search, market, and minimum-price controls filter the complete ranking
without changing forecast values or global ranks. Displayed names include exact
variant details such as gem level/quality/corruption, Forbidden Flame versus
Flesh and its passive, base or cluster item level, influence, and socket links.
Results are displayed in pages of 25, 50, or 100 rows. Price thresholds are
cumulative: for example, `10d+` includes every item priced at 10 Divine Orbs or
more.

The **Sections** checklist can independently include or exclude every ranked
market category—for example, uncheck `Forbidden Jewel` to remove all Forbidden
Flame and Forbidden Flesh rows. Section exclusions are saved in browser-local
storage, combine with every other filter, and can be cleared with **Include
all**. Newly introduced categories default to included.

Every visible row includes a generated link to the official trade site for the
current league. Skill-gem links carry the archived level, quality, and
corruption variant when available. Forbidden-jewel links select Flame or Flesh
but are labelled as a base search because the passive still needs to be chosen
in the trade site's Allocates filter.

`Hide` removes one exact item variant from the ranking on this browser. The
hide list is stored in browser-local storage and applies across pages, searches,
filters, syncs, and reloads. `Reset hidden` restores the complete list.

Open any row to compare its exact current-league Divine price curve with the
recency-weighted curve built only from Mirage, Keepers, Mercenaries, and
Settlers. Charts begin at league day 1, break across missing days, and never
copy day 2 backward or infer dates from poe.ninja sparklines.

These are gross price forecasts, not guaranteed profits. Verify the live
market, exact item variant, corruption state, rolls, and available depth before
trading.

## Commands

```powershell
# Start the server and open the browser
python -m poe_advisor serve --open

# Start on a different interface/port
python -m poe_advisor serve --host 127.0.0.1 --port 8787

# Run one live sync
python -m poe_advisor sync

# Opt in to a bounded official hourly audit backfill
python -m poe_advisor sync --history-hours 24

# Import any official completed-league dumps not already checkpointed
python -m poe_advisor seasonal-sync --items 80

# Explicitly seed the labelled offline fixture
python -m poe_advisor seed-demo

# Print the ranking sorted by 7-day gross forecast
python -m poe_advisor recommend --horizon 7
```

## HTTP API

- `GET /api/health` — process and database health.
- `GET /api/status` — league, last sync, database counts, source states, and
  demo/live status.
- `POST /api/sync` — one-click sync; accepts `backfill_hours` from `0` to
  `336` plus optional `horizon`. One ranking run is retained after a successful
  sync. Legacy `budget` input is accepted but cannot affect ranking.
- `POST /api/seasonal/backfill` — resumable official poe.ninja dump import;
  legacy `max_items` input is accepted for compatibility but cannot truncate
  a league ZIP.
- `GET /api/recommendations?horizon=7` — every exact ranked item variant sorted
  by the selected gross forecast. The response uses compact rows so the browser
  can paginate and filter the complete ranked universe locally. Each row exposes
  `forecast_3d` / `forecast_7d` / `forecast_14d`, with `expected_gain_pct`,
  `historical_target_price_divine`, `historical_target_gain_pct`,
  `historical_sample_leagues`, `historical_leagues`, and
  `current_curve_projection.capped_gain_pct`.
- `GET /api/history?key=<item-key>` — current and recency-weighted historical
  price curves and explicit day-by-day coverage. The displayed weighted curve
  contains only the four broadly covered leagues and includes exact Low rows;
  forecast targets use a separate Medium/High-only weighted series.
- `GET /api/settings` — non-secret settings.
- `POST /api/settings` — update the permitted public settings.

The same server serves `/`, `/styles.css`, `/app.js`, and `/og.png` from
`web/`.

## Environment settings

- `POE_ADVISOR_DB` — SQLite database path.
- `POE_ADVISOR_CONTACT` — contact identifier placed in the polite API
  User-Agent. Set this to an email or project URL before sustained use.
- `POE_ADVISOR_TIMEOUT` — HTTP timeout in seconds; default `20`.
- `POE_ADVISOR_RETRIES` — retry count for transient failures; default `2`.
- `POE_ADVISOR_MAX_RETRY_AFTER` — maximum wait honored per retry; default
  `30` seconds.
- `POE_ADVISOR_COMPACT_HISTORY` — opt in to integer-keyed completed-league
  storage (`1`, `true`, `yes`, or `on`). The hosted workflow enables it; local
  imports leave it disabled by default so the full research archive is kept.
- `POE_OAUTH_TOKEN` — optional service token for official league metadata.
  It is not needed for poe.ninja or the public GGG hourly exchange feed and is
  never returned by the settings API.

Command-line `--host` and `--port` flags override the server defaults.

## Tests

```powershell
python -m unittest discover -s tests_python -v
```

The backend is Python-standard-library-only, so `pip install -r
requirements.txt` is optional and performs no package downloads.

## Data sources and disclaimer

All current and historical price inputs used by the forecast come from
poe.ninja: current economy overviews, exact current detail histories, and the
official completed-league ZIP dumps published at
[poe.ninja/poe1/data](https://poe.ninja/poe1/data). The application also uses
poe.ninja's build-composition pages, plus the official GGG Currency Exchange,
experience-ladder, and passive-tree sources as separately labelled archive or
metadata context. None of those contextual sources can replace a poe.ninja
price. poe.watch is optional metadata/legacy archive only and is never a
recommendation price source. Requests use conditional caching, an identifiable
User-Agent, bounded retries, and `Retry-After` handling.

This product isn't affiliated with or endorsed by Grinding Gear Games in any
way. Path of Exile and all related names are trademarks of Grinding Gear Games.
Market data can be delayed, incomplete, manipulated, or unavailable. No model
can guarantee appreciation, liquidity, or execution at a displayed price. Use
the tool at your own risk and never risk currency you cannot afford to lose.
