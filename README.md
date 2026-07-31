# Wraeclast Ledger

Wraeclast Ledger is a local-first Path of Exile 1 softcore-trade market
research tool. It downloads current economy data, retains the source responses
and normalized price history in SQLite, aligns the same item by league day
across completed leagues, compares today's price with a recency-weighted
future historical level, blends it with the current-league price curve, and
produces gross 3, 7, and 14-day gain forecasts. The selected horizon determines
the global order shown across paginated results. The archive is intentionally
broad, while routine low-end
consumable categories and base currencies remain outside the displayed
investment universe.

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
continue past-league archive**. Forecasts use exact item/day observations from
the four broadly covered softcore trade leagues: Mirage, Keepers (also
reported upstream as Keepers of the Flame), Mercenaries, and Settlers. The
backfill is resumable, so later passes skip successful item/league histories.

## GitHub Pages and automatic daily updates

The same dashboard can run as a read-only GitHub Pages site without exposing
the local API or a raw copy of the local SQLite database. The included
`.github/workflows/daily-pages.yml` workflow:

1. Restores the most recent sanitized, compressed market database from the
   `market-archive` GitHub Release.
2. Runs the full unattended refresh: current prices, optional build
   composition, top-ranked current-league curves, a resumable completed-league
   batch, and the recommendation model.
3. Exports one all-item catalog with exact 3/7/14-day ranks plus 256 lazily
   loaded, content-hashed curve shards.
4. Creates an integrity-checked, public-market-only SQLite backup and rotates
   the prior good backup as `poe_market_history.previous.sqlite3.gz`. Raw HTTP
   payloads, local diagnostics, and settings outside a narrow crawl allowlist
   are removed before compression.
5. Deploys only the static dashboard data to GitHub Pages.

The scheduled run starts daily at 09:23 in `America/Los_Angeles`. It can also
be started manually from the repository's **Actions** tab. On GitHub Pages,
the dashboard's primary update button opens that workflow; it never embeds a
GitHub token in browser code. A small successful-run heartbeat is committed so
public-repository schedules do not become dormant after 60 days. A failed
refresh does not replace the previous Pages deployment or archive snapshot.

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
  --output build/archive/poe_market_history.sqlite3.gz `
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
- Every successful raw HTTP response, gzip-compressed and content-hashed.
- Divine-relative normalized price observations.
- The full poe.watch item catalog used for historical discovery.
- Completed-league daily prices, fetch progress, volume, and confidence.
- Exact dated current-league poe.watch histories for ranked items, including
  the raw item and same-day Divine Orb responses used for normalization.
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
responses do not duplicate them.

## Sync and backfill behavior

The **Sync market & run model** button calls `POST /api/sync`. A normal sync:

1. Discovers the current PoE 1 softcore trade league from poe.ninja.
2. Optionally enriches the league start time from poe.watch.
3. Uses conditional requests to fetch the configured full PoE 1 poe.ninja
   universe by default: 18 exchange categories and 25 item-overview
   categories. This includes currencies, fragments, divination cards,
   crafting consumables, uniques, gems, maps, bases, beasts, and the other
   currently documented modalities. Collection scope is broader than
   recommendation scope, so excluded consumables still accumulate local
   history.
4. Stores the full poe.watch catalog and derives current prices for every
   exact Forbidden Flesh/Flame passive variant.
5. Stores GGG's official passive-tree export and uses it to map those exact
   variants to ascendancies; ambiguous or unknown passives are left unadjusted.
6. Stores each raw response before writing normalized price points and leaves
   existing local data intact when one source or category fails.
7. Archives poe.ninja's indexed-build class distribution and its nearest
   available past-league time-machine snapshots. The much smaller official
   experience-ladder sample is used only when poe.ninja is unavailable.
8. Runs a preliminary top-100 ranking, matches those exact item identities in
   the poe.watch catalog, and archives their dated current-league histories.
   Prices are normalized only when that item day has an exact same-day Divine
   Orb observation; missing trades and missing anchors remain gaps.
9. Regenerates and stores the final ranking after the curve archive is updated.

The optional JSON field `backfill_hours` also advances the official GGG
Currency Exchange hourly archive:

```json
{"backfill_hours": 24}
```

The official feed is historical, mixes all leagues, and does not expose the
current hour. Wraeclast Ledger filters the selected league locally and saves a
resume cursor. Backfill is intentionally bounded; it does not crawl the entire
multi-year archive in one click.

Recent official hourly payloads can be roughly 1–2 MB each before local gzip
compression. A 24-hour backfill may therefore download tens of megabytes and
take several minutes depending on rate limits and connection speed. The first
poe.ninja sync is normally much smaller. Repeated syncs are cheaper because
ETag and immutable-snapshot caching are used.

The separate completed-league button calls `POST /api/seasonal/backfill`. Each
pass:

1. Saves the full current poe.watch compact catalog as a compressed raw
   snapshot.
2. Prioritizes liquid fungible markets that can be matched exactly to the
   current poe.ninja catalog.
3. Fetches the selected item in the six reviewed leagues: Affliction,
   Necropolis, Settlers, Mercenaries, Keepers, and Mirage.
4. Fetches each league's Divine Orb curve and converts every Chaos-denominated
   historical row to Divine Orbs.
5. Stores one consolidated row per item and league day, plus raw responses and
   resumable success/failure state.
6. Archives the nearest available completed-league poe.ninja build snapshots
   for the meta-demand baseline, with official ladder pages as fallback.

The default UI batch is 80 assets. Use repeated passes to extend breadth; no
successful history is downloaded twice.

## Historical-data limitations

poe.ninja's supported public API exposes current economy overviews only.
Sparkline values are relative, untimestamped trend samples, so Wraeclast Ledger
does not invent dated observations from them. Item history becomes stronger as
you run real syncs over time.

The official GGG Currency Exchange feed provides genuine hourly historical
digests for fungible markets. Old hours may eventually be removed upstream,
which is why downloaded snapshots are retained locally.

poe.watch supplies documented dated item history for known numeric item IDs.
It does not reliably enumerate completed leagues or their old item catalogs,
so the application maintains an explicit reviewed six-league calendar and can
only backfill an item whose stable ID is discoverable from the current
catalog. Affliction through Mirage have usable early-league Divine histories
under the stable current ID; older aliases do not and are excluded rather than
misaligned. Missing rows mean “no usable evidence,” never a zero price.
Skill-gem history is matched on name, level, quality, and corruption state;
incomplete variants fail closed so a level-1 uncorrupted gem is never blended
with a level-4 or corrupted listing.

Historical item prices arrive in Chaos, so each league-day needs a trustworthy
Chaos-per-Divine anchor. The importer sanity-checks the raw Divine Orb curve,
removes unsafe derived rows for implausible days while retaining the original
raw response, and never silently borrows an adjacent day. A sparse direct
anchor is also rejected when at least two independent direct league curves
tightly agree and it is more than eight times away. A missing anchor can be
rebuilt only from the median for that exact league day in at least two other
validated leagues. Those fallback rows record their donor leagues and values
and receive deliberately low confidence.

If an item lacks an exact future-day price in the broadly covered leagues, the
affected horizon is shown as `—` with “no exact broad-league future-day
price.” Missing evidence is never converted to a 0% return.

poe.ninja build distributions and official ladder samples remain in the local
archive for separate market research. They do not alter the forecast ranking.

## Forecast model

### Investment universe

Essence, Fossil, Oil, Resonator, Scarab, Delirium Orb, Artifact, and Incubator
categories remain outside the displayed ranking, as requested. Routine base
currencies—scrolls, scraps, quality currency, and common crafting orbs such as
Jeweller's, Fusing, Alteration, Chromatic, Regret, Vaal, Chaos, and Exalted
Orbs—are also outside the ranking. This is an explicit investment-universe
choice, not a price or budget cap. Premium currencies such as Fracturing,
Sacred, and Veiled Orbs, Hinekora's Locks, and Mirrors remain in scope. All
excluded prices are still archived locally for research and fast queries. No
individual-item lifecycle rule removes an otherwise in-scope item.

### Broad-league evidence

Forecast calculations use only the four broadly covered leagues:

- Mirage
- Keepers (including the upstream name `Keepers of the Flame`)
- Mercenaries
- Settlers

Affliction and Necropolis remain stored but do not contribute to forecasts.
For each item and horizon, the model reads the exact historical price at the
current league day plus 3, 7, or 14 days. That recency-weighted future price is
compared directly with today's current-league price; a historical entry price
is not required. The future-day observation count is shown per horizon.
Missing observations stay missing; the dashboard displays `—` and “no exact
broad-league future-day price,” never 0%.

The available broad-league future prices are recency weighted. Newer leagues
receive more influence than older leagues, and the available weights are
renormalized to 100% rather than treating a missing league as a zero-price
observation.

### Gross expected gain

For each horizon:

1. Calculate the recency-weighted historical future price level from exact
   broad-league future-day observations.
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

The output does not deduct transaction friction or adjust for liquidity,
confidence scores, falling-knife behavior, disagreement penalties, Standard
prices, build demand, or structural-item rules. There are no per-item
screening gates. A negative forecast remains visible and ranks below a higher
forecast.

The chosen 3, 7, or 14-day hold window is the sort horizon. Every row still
shows all three estimates, their historical targets, current-curve components,
and broad-league sample counts in the detail view. Budget, item price, and
portfolio allocation do not affect the order.

The search, market, and minimum-price controls filter the complete ranking
without changing forecast values or global ranks. Results are displayed in
pages of 25, 50, or 100 rows. Price thresholds are cumulative: for example,
`10d+` includes every item priced at 10 Divine Orbs or more.

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

# Include a bounded official hourly backfill
python -m poe_advisor sync --history-hours 24

# Build the next 80 common completed-league item histories
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
- `POST /api/seasonal/backfill` — resumable completed-league item backfill;
  accepts `max_items` from `1` to `2000` plus optional `horizon`.
- `GET /api/recommendations?horizon=7` — every exact ranked item variant sorted
  by the selected gross forecast. The response uses compact rows so the browser
  can paginate and filter the complete universe locally. Each row exposes
  `forecast_3d` / `forecast_7d` / `forecast_14d`, with `expected_gain_pct`,
  `historical_target_price_divine`, `historical_target_gain_pct`,
  `historical_sample_leagues`, `historical_leagues`, and
  `current_curve_projection.capped_gain_pct`.
- `GET /api/history?key=<item-key>` — current and recency-weighted historical
  price curves and explicit day-by-day coverage. The weighted curve used by
  the forecast contains only the four broadly covered leagues.
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

Wraeclast Ledger uses the supported poe.ninja economy endpoints, poe.ninja's
server-rendered build-composition and time-machine pages, the official GGG
historical Currency Exchange feed, official public experience-ladder pages
as fallback, GGG's official passive-tree export, and documented poe.watch
league, catalog, and item-history endpoints. It uses conditional caching, an
identifiable User-Agent, bounded retries, and `Retry-After` handling. Do not
configure it to poll faster than upstream cache windows.

This product isn't affiliated with or endorsed by Grinding Gear Games in any
way. Path of Exile and all related names are trademarks of Grinding Gear Games.
Market data can be delayed, incomplete, manipulated, or unavailable. No model
can guarantee appreciation, liquidity, or execution at a displayed price. Use
the tool at your own risk and never risk currency you cannot afford to lose.
