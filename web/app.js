"use strict";

const state = {
  status: null,
  payload: null,
  recommendations: [],
  syncing: false,
  historySyncing: false,
  excludedCategories: new Set(),
  availableCategories: [],
  priceRange: "all",
  query: "",
  page: 1,
  pageSize: 50,
  hiddenItemKeys: new Set(),
};

const runtime = {
  mode: "local",
  manifest: null,
  catalogPromise: null,
  rankingIndexPromise: null,
  rankingPagePromises: new Map(),
  catalogRows: null,
  normalizedRecommendations: null,
  pageRenderSignature: null,
  pageRenderSequence: 0,
  historyShardPromises: new Map(),
};

const els = {
  statusDot: document.querySelector("#statusDot"),
  leagueName: document.querySelector("#leagueName"),
  leagueDay: document.querySelector("#leagueDay"),
  lastSync: document.querySelector("#lastSync"),
  databaseMeta: document.querySelector("#databaseMeta"),
  trustStrip: document.querySelector("#trustStrip"),
  trustMessage: document.querySelector("#trustMessage"),
  trustAction: document.querySelector("#trustAction"),
  horizonSelect: document.querySelector("#horizonSelect"),
  syncButton: document.querySelector("#syncButton"),
  syncButtonLabel: document.querySelector("#syncButtonLabel"),
  forecastHeaders: document.querySelectorAll("[data-forecast-header]"),
  modelNote: document.querySelector("#modelNote"),
  recommendationList: document.querySelector("#recommendationList"),
  categoryFilter: document.querySelector("#categoryFilter"),
  categoryFilterSummary: document.querySelector("#categoryFilterSummary"),
  categoryChecklist: document.querySelector("#categoryChecklist"),
  includeAllCategoriesButton: document.querySelector("#includeAllCategoriesButton"),
  excludeAllCategoriesButton: document.querySelector("#excludeAllCategoriesButton"),
  priceRangeFilter: document.querySelector("#priceRangeFilter"),
  searchInput: document.querySelector("#searchInput"),
  resetHiddenItemsButton: document.querySelector("#resetHiddenItemsButton"),
  hiddenItemCount: document.querySelector("#hiddenItemCount"),
  paginationControls: document.querySelector("#paginationControls"),
  paginationSummary: document.querySelector("#paginationSummary"),
  previousPageButton: document.querySelector("#previousPageButton"),
  nextPageButton: document.querySelector("#nextPageButton"),
  pageSizeSelect: document.querySelector("#pageSizeSelect"),
  snapshotCount: document.querySelector("#snapshotCount"),
  pricePointCount: document.querySelector("#pricePointCount"),
  exchangeHourCount: document.querySelector("#exchangeHourCount"),
  historicalAssetCount: document.querySelector("#historicalAssetCount"),
  seasonalPriceCount: document.querySelector("#seasonalPriceCount"),
  archiveSize: document.querySelector("#archiveSize"),
  archiveEyebrow: document.querySelector("#archiveEyebrow"),
  dataTitle: document.querySelector("#dataTitle"),
  archiveDescription: document.querySelector("#archiveDescription"),
  detailDialog: document.querySelector("#detailDialog"),
  detailContent: document.querySelector("#detailContent"),
  detailClose: document.querySelector("#dialogClose"),
  settingsDialog: document.querySelector("#settingsDialog"),
  settingsButton: document.querySelector("#settingsButton"),
  settingsClose: document.querySelector("#settingsClose"),
  settingsTitle: document.querySelector("#settingsTitle"),
  settingsIntro: document.querySelector("#settingsIntro"),
  sourceList: document.querySelector("#sourceList"),
  backfillSelect: document.querySelector("#backfillSelect"),
  historyBatchSelect: document.querySelector("#historyBatchSelect"),
  historyButton: document.querySelector("#historyButton"),
  historyMeta: document.querySelector("#historyMeta"),
  refreshStatusButton: document.querySelector("#refreshStatusButton"),
  toastRegion: document.querySelector("#toastRegion"),
};

async function fetchJson(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });

  let body;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!response.ok) {
    const message = body?.detail || body?.message || `Request failed (${response.status})`;
    throw new Error(message);
  }
  return body;
}

function staticAssetUrl(relativePath) {
  return new URL(
    String(relativePath || "").replace(/^\/+/, ""),
    document.baseURI,
  ).toString();
}

async function discoverRuntime() {
  const response = await fetch(
    staticAssetUrl("data/manifest.json"),
    { cache: "no-store" },
  );
  if (response.status === 404) return;
  if (!response.ok) {
    throw new Error(`Published data manifest failed (${response.status}).`);
  }
  const manifest = await response.json();
  if (
    ![1, 2].includes(manifest?.schema_version)
    || manifest?.mode !== "github-pages"
    || !manifest?.status
    || !manifest?.catalog
    || (
      manifest?.schema_version >= 2
      && (
        !manifest?.ranking_index
        || !manifest?.ranking_pages?.page_size
        || !manifest?.ranking_pages?.horizons
      )
    )
  ) {
    runtime.mode = "static";
    throw new Error("Published data manifest is invalid or unsupported.");
  }
  runtime.mode = "static";
  runtime.manifest = manifest;
}

async function staticCatalog() {
  if (!runtime.catalogPromise) {
    runtime.catalogPromise = fetchJson(
      staticAssetUrl(runtime.manifest.catalog),
      { cache: "force-cache" },
    );
  }
  return runtime.catalogPromise;
}

function decodeStaticRankingIndex(payload) {
  if (!Array.isArray(payload?.fields) || !Array.isArray(payload?.items)) {
    throw new Error("Published ranking index is invalid.");
  }
  const positions = Object.fromEntries(
    payload.fields.map((field, index) => [String(field), index]),
  );
  const value = (entry, field) => entry[positions[field]];
  return payload.items.map((entry) => {
    if (!Array.isArray(entry)) {
      throw new Error("Published ranking index row is invalid.");
    }
    return {
      key: String(value(entry, "key") || ""),
      name: String(value(entry, "name") || "Unknown item"),
      category: String(value(entry, "category") || "Other"),
      search_text: String(value(entry, "search_text") || "").toLowerCase(),
      price_divine: value(entry, "price_divine"),
      price_chaos: value(entry, "price_chaos"),
      static_ranks: {
        "3": value(entry, "rank_3d"),
        "7": value(entry, "rank_7d"),
        "14": value(entry, "rank_14d"),
      },
    };
  });
}

async function staticRankingIndex() {
  if (!runtime.rankingIndexPromise) {
    runtime.rankingIndexPromise = fetchJson(
      staticAssetUrl(runtime.manifest.ranking_index),
      { cache: "force-cache" },
    ).then(decodeStaticRankingIndex);
  }
  return runtime.rankingIndexPromise;
}

async function staticRankingPage(horizon, pageNumber) {
  const paths = runtime.manifest?.ranking_pages?.horizons?.[String(horizon)];
  const shardPath = Array.isArray(paths) ? paths[pageNumber - 1] : null;
  if (!shardPath) {
    throw new Error(`Published ${horizon}-day ranking page ${pageNumber} is missing.`);
  }
  if (!runtime.rankingPagePromises.has(shardPath)) {
    runtime.rankingPagePromises.set(
      shardPath,
      fetchJson(staticAssetUrl(shardPath), { cache: "force-cache" }),
    );
  }
  return runtime.rankingPagePromises.get(shardPath);
}

async function loadStaticPageDetails(items, horizon) {
  if (runtime.manifest?.schema_version < 2 || !items.length) return items;
  const shardSize = Math.max(
    1,
    toNumber(runtime.manifest?.ranking_pages?.page_size, 100),
  );
  const pageNumbers = [...new Set(items.map((item) => (
    Math.floor((Math.max(1, toNumber(item.rank, 1)) - 1) / shardSize) + 1
  )))];
  const pages = await Promise.all(
    pageNumbers.map((pageNumber) => staticRankingPage(horizon, pageNumber)),
  );
  const rowByKey = new Map();
  for (const page of pages) {
    for (const row of Array.isArray(page?.items) ? page.items : []) {
      const itemKey = String(row?.key || row?.item_key || row?.curve_key || "");
      if (itemKey) rowByKey.set(itemKey, row);
    }
  }
  for (const item of items) {
    const row = rowByKey.get(item.itemKey);
    if (!row) {
      throw new Error(`Published ranking row is missing for ${item.itemKey}.`);
    }
    const preservedRank = item.rank;
    const preservedSearch = item.searchText;
    Object.assign(item, normalizeRecommendation(row, preservedRank - 1));
    item.rank = preservedRank;
    item.searchText = preservedSearch;
  }
  return items;
}

async function staticHistory(itemKey) {
  const item = state.recommendations.find(
    (candidate) => candidate.itemKey === itemKey,
  );
  const shard = item?.history_shard || item?.historyShard;
  const shardPath = shard
    ? runtime.manifest?.history_shards?.[String(shard)]
    : null;
  if (!shardPath) {
    return { seasonal_comparison: null };
  }
  if (!runtime.historyShardPromises.has(shardPath)) {
    runtime.historyShardPromises.set(
      shardPath,
      fetchJson(staticAssetUrl(shardPath), { cache: "force-cache" }),
    );
  }
  const payload = await runtime.historyShardPromises.get(shardPath);
  return {
    seasonal_comparison: payload?.items?.[itemKey] || null,
  };
}

async function api(path, options = {}) {
  if (runtime.mode !== "static") {
    return fetchJson(path, options);
  }
  const method = String(options.method || "GET").toUpperCase();
  if (method !== "GET") {
    throw new Error(
      "The published dashboard is read-only. Run its GitHub update workflow instead.",
    );
  }
  const parsed = new URL(path, "https://wraeclast-ledger.invalid");
  if (parsed.pathname === "/api/status") {
    return fetchJson(
      staticAssetUrl(runtime.manifest.status),
      { cache: "force-cache" },
    );
  }
  if (parsed.pathname === "/api/recommendations") {
    const horizon = toNumber(parsed.searchParams.get("horizon"), 7);
    if (runtime.manifest?.schema_version >= 2) {
      const [catalog, rankings] = await Promise.all([
        staticCatalog(),
        staticRankingIndex(),
      ]);
      return { ...catalog, rankings, horizon };
    }
    const catalog = await staticCatalog();
    return { ...catalog, horizon };
  }
  if (parsed.pathname === "/api/history") {
    return staticHistory(parsed.searchParams.get("key") || "");
  }
  throw new Error(`Published data route is unavailable: ${parsed.pathname}`);
}

function toNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function nullableNumber(...values) {
  for (const value of values) {
    if (value == null || value === "") continue;
    const number = Number(value);
    if (Number.isFinite(number)) return number;
  }
  return null;
}

function money(value, digits = 1) {
  const number = toNumber(value);
  if (number >= 1000) return `${Intl.NumberFormat("en", { maximumFractionDigits: 0 }).format(number)} div`;
  return `${number.toFixed(number < 10 ? Math.max(digits, 2) : digits)} div`;
}

function percent(value, digits = 1) {
  let number = toNumber(value);
  if (Math.abs(number) <= 1) number *= 100;
  return `${number >= 0 ? "+" : ""}${number.toFixed(digits)}%`;
}

function unsignedPercent(value, digits = 1) {
  let number = toNumber(value);
  if (Math.abs(number) <= 1) number *= 100;
  return `${Math.abs(number).toFixed(digits)}%`;
}

function compact(value) {
  return Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(toNumber(value));
}

function bytes(value) {
  const number = toNumber(value);
  if (!number) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const index = Math.min(Math.floor(Math.log(number) / Math.log(1024)), units.length - 1);
  return `${(number / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function relativeTime(value) {
  if (!value) return "No local snapshot yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const abs = Math.abs(seconds);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  if (abs < 60) return `Synced ${formatter.format(seconds, "second")}`;
  if (abs < 3600) return `Synced ${formatter.format(Math.round(seconds / 60), "minute")}`;
  if (abs < 86400) return `Synced ${formatter.format(Math.round(seconds / 3600), "hour")}`;
  return `Synced ${formatter.format(Math.round(seconds / 86400), "day")}`;
}

function normalizeStatus(raw = {}) {
  const db = raw.database || {};
  const leagueRaw = typeof raw.league === "string" ? { id: raw.league, name: raw.league } : raw.league || {};
  return {
    league: {
      id: leagueRaw.id || raw.league_id || "Unknown league",
      name: leagueRaw.name || leagueRaw.id || raw.league_name || "Unknown league",
      day: toNumber(leagueRaw.day ?? raw.league_day, 0),
    },
    lastSync: raw.last_sync_at || raw.last_sync || null,
    demo: Boolean(raw.demo_mode || raw.mode === "demo"),
    syncing: Boolean(raw.syncing),
    database: {
      snapshots: db.total_snapshots ?? db.snapshots ?? raw.snapshot_count ?? 0,
      prices: db.price_points ?? db.prices ?? raw.price_point_count ?? 0,
      exchangeHours: db.exchange_hours ?? raw.exchange_hour_count ?? 0,
      size: db.size_bytes ?? raw.database_size_bytes ?? 0,
    },
    seasonal: {
      catalogAssets: db.catalog_assets ?? raw.seasonal?.catalog_assets ?? 0,
      prices: db.seasonal_prices ?? raw.seasonal?.seasonal_prices ?? 0,
      leagues: db.historical_leagues ?? raw.seasonal?.historical_leagues ?? 0,
      completedFetches: db.completed_fetches ?? raw.seasonal?.completed_fetches ?? 0,
      usableFetches: db.usable_fetches ?? raw.seasonal?.usable_fetches
        ?? db.completed_fetches ?? raw.seasonal?.completed_fetches ?? 0,
    },
    meta: {
      available: Boolean(raw.meta?.available),
      sampleSize: raw.meta?.sample_size ?? 0,
      leagueDay: raw.meta?.league_day ?? null,
      observedAt: raw.meta?.observed_at ?? null,
      profiles: raw.meta?.profiles ?? 0,
      caveat: raw.meta?.caveat || "",
    },
    sources: Array.isArray(raw.sources) ? raw.sources : [],
    historySyncing: Boolean(raw.history_syncing),
  };
}

function setSourceState(kind, message) {
  els.trustStrip.classList.remove("live", "warning", "error");
  els.statusDot.classList.remove("live", "warning", "error");
  els.trustStrip.classList.add(kind);
  els.statusDot.classList.add(kind);
  els.trustMessage.textContent = message;
}

function renderStatus() {
  const status = state.status;
  if (!status) return;

  els.leagueName.textContent = status.league.name;
  els.leagueDay.textContent = status.league.day > 0 ? `Day ${status.league.day}` : "Day —";
  els.lastSync.textContent = relativeTime(status.lastSync);
  const archiveLabel = runtime.mode === "static"
    ? "Published archive"
    : "Local database";
  els.databaseMeta.textContent = `${archiveLabel} · ${compact(status.database.prices + status.seasonal.prices)} prices`;
  els.snapshotCount.textContent = compact(status.database.snapshots);
  els.pricePointCount.textContent = compact(status.database.prices);
  els.exchangeHourCount.textContent = compact(status.database.exchangeHours);
  els.historicalAssetCount.textContent = compact(status.seasonal.catalogAssets);
  els.seasonalPriceCount.textContent = compact(status.seasonal.prices);
  els.archiveSize.textContent = bytes(status.database.size);
  els.historyMeta.textContent = status.seasonal.prices
    ? `${compact(status.seasonal.prices)} league-day bars from ${status.seasonal.leagues} archived leagues; ${compact(status.seasonal.usableFetches)} item/league histories usable (${compact(status.seasonal.completedFetches)} fully direct). Forecasts use Mirage, Keepers, Mercenaries, and Settlers only.`
    : "No completed-league bars yet. Build the seasonal archive after the first live market sync.";
  els.historyButton.disabled = status.historySyncing || state.historySyncing;

  const unavailableStates = ["error", "offline", "failed", "unavailable"];
  const failing = status.sources.filter(
    (source) => source.required !== false && unavailableStates.includes(String(source.status).toLowerCase()),
  );
  const degraded = status.sources.filter((source) => {
    const sourceStatus = String(source.status).toLowerCase();
    return ["warning", "stale", "limited"].includes(sourceStatus)
      || (source.required === false && unavailableStates.includes(sourceStatus));
  });

  if (status.demo) {
    setSourceState("warning", "Demo data is loaded. Run Sync market to replace it with a clearly timestamped live snapshot.");
  } else if (failing.length) {
    setSourceState("error", `${failing.length} source${failing.length > 1 ? "s are" : " is"} unavailable. Existing local history remains readable.`);
  } else if (!status.lastSync) {
    setSourceState("warning", "Your archive is ready but empty. Run the first sync to discover the current softcore league and build today’s snapshot.");
  } else if (status.seasonal.leagues < 3 || status.seasonal.prices === 0) {
    setSourceState("warning", "Live prices are current, but broad-league coverage is not ready. Build the past-league archive to calculate forecasts.");
  } else if (degraded.length) {
    setSourceState("warning", "Latest data is usable with caveats. Open settings for source-specific details.");
  } else if (runtime.mode === "static") {
    setSourceState("live", `Published market data was updated ${new Date(status.lastSync).toLocaleString()}. GitHub refreshes it daily; live listings can move between updates.`);
  } else {
    setSourceState("live", `Local history is current as of ${new Date(status.lastSync).toLocaleString()}. Live listings can still move after a sync.`);
  }

  els.trustAction.hidden = status.sources.length === 0;
  renderSources();
}

const FORECAST_HORIZONS = [3, 7, 14];
const BROAD_LEAGUES = ["Mirage", "Keepers", "Mercenaries", "Settlers"];
const HIDDEN_ITEMS_STORAGE_KEY = "wraeclast-ledger.hidden-items.v1";
const CATEGORY_EXCLUSIONS_STORAGE_KEY = "wraeclast-ledger.excluded-categories.v1";
const PRICE_THRESHOLDS = {
  "10c-plus": { field: "priceChaos", minimum: 10 },
  "1d-plus": { field: "priceDivine", minimum: 1 },
  "5d-plus": { field: "priceDivine", minimum: 5 },
  "10d-plus": { field: "priceDivine", minimum: 10 },
  "20d-plus": { field: "priceDivine", minimum: 20 },
  "50d-plus": { field: "priceDivine", minimum: 50 },
  "100d-plus": { field: "priceDivine", minimum: 100 },
};
const UNIQUE_TRADE_CATEGORIES = new Set([
  "UniqueAccessory",
  "UniqueArmour",
  "UniqueFlask",
  "UniqueJewel",
  "UniqueRelic",
  "UniqueWeapon",
]);
// Exact user-facing trade links only; prices and forecasts still come from
// poe.ninja. These stat IDs identify the rolled explicit modifiers on the
// official Path of Exile trade search.
const VOICES_PASSIVE_STAT_ID = "explicit.stat_1085446536";
const ADORNED_EFFECT_STAT_ID = "explicit.stat_461663422";
const ADORNED_HIGH_ROLL_MINIMUM = 90;
const BROAD_LEAGUE_ALIASES = new Map([
  ["mirage", "Mirage"],
  ["keepers", "Keepers"],
  ["keepers of the flame", "Keepers"],
  ["mercenaries", "Mercenaries"],
  ["mercenaries of trarthus", "Mercenaries"],
  ["settlers", "Settlers"],
  ["settlers of kalguur", "Settlers"],
]);

function loadHiddenItems() {
  try {
    const raw = window.localStorage.getItem(HIDDEN_ITEMS_STORAGE_KEY);
    if (!raw) {
      state.hiddenItemKeys = new Set();
      return;
    }
    const values = JSON.parse(raw);
    if (!Array.isArray(values)) throw new TypeError("Hidden item list is not an array.");
    state.hiddenItemKeys = new Set(
      values
        .filter((value) => typeof value === "string")
        .map((value) => value.trim())
        .filter(Boolean),
    );
  } catch {
    state.hiddenItemKeys = new Set();
  }
}

function saveHiddenItems() {
  try {
    window.localStorage.setItem(
      HIDDEN_ITEMS_STORAGE_KEY,
      JSON.stringify([...state.hiddenItemKeys]),
    );
    return true;
  } catch {
    return false;
  }
}

function loadCategoryExclusions() {
  try {
    const raw = window.localStorage.getItem(CATEGORY_EXCLUSIONS_STORAGE_KEY);
    if (!raw) {
      state.excludedCategories = new Set();
      return;
    }
    const values = JSON.parse(raw);
    if (!Array.isArray(values)) {
      throw new TypeError("Category exclusion list is not an array.");
    }
    state.excludedCategories = new Set(
      values
        .filter((value) => typeof value === "string")
        .map((value) => value.trim())
        .filter(Boolean),
    );
  } catch {
    state.excludedCategories = new Set();
  }
}

function saveCategoryExclusions() {
  try {
    window.localStorage.setItem(
      CATEGORY_EXCLUSIONS_STORAGE_KEY,
      JSON.stringify([...state.excludedCategories].sort()),
    );
    return true;
  } catch {
    return false;
  }
}

function categoryLabel(category) {
  return String(category || "Other")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ");
}

function activeExcludedCategoryCount() {
  return state.availableCategories.reduce(
    (count, category) => count + (state.excludedCategories.has(category) ? 1 : 0),
    0,
  );
}

function renderCategoryFilterSummary() {
  const total = state.availableCategories.length;
  const excluded = activeExcludedCategoryCount();
  const included = total - excluded;
  if (els.categoryFilterSummary) {
    els.categoryFilterSummary.textContent = !total || excluded === 0
      ? "All included"
      : included === 0
        ? "None included"
        : `${compact(included)} of ${compact(total)} included`;
  }
  if (els.includeAllCategoriesButton) {
    els.includeAllCategoriesButton.disabled = state.excludedCategories.size === 0;
  }
  if (els.excludeAllCategoriesButton) {
    els.excludeAllCategoriesButton.disabled = total === 0 || excluded === total;
  }
}

function applyCategorySelection() {
  state.page = 1;
  const persisted = saveCategoryExclusions();
  renderCategoryFilterSummary();
  renderRankingSummary();
  renderRecommendations();
  if (!persisted) {
    toast("Section choices could not be saved; they apply to this session only.", "error");
  }
}

function setCategoryIncluded(category, included) {
  if (!category) return;
  if (included) {
    state.excludedCategories.delete(category);
  } else {
    state.excludedCategories.add(category);
  }
  applyCategorySelection();
}

function includeAllCategories() {
  if (!state.excludedCategories.size) return;
  state.excludedCategories.clear();
  renderCategoryOptions();
  applyCategorySelection();
}

function excludeAllCategories() {
  if (!state.availableCategories.length) return;
  state.excludedCategories = new Set(state.availableCategories);
  renderCategoryOptions();
  applyCategorySelection();
}

function currentHiddenCount() {
  return state.recommendations.reduce(
    (count, item) => count + (state.hiddenItemKeys.has(item.itemKey) ? 1 : 0),
    0,
  );
}

function renderHiddenItemsControl() {
  if (!els.resetHiddenItemsButton || !els.hiddenItemCount) return;
  const storedCount = state.hiddenItemKeys.size;
  const activeCount = currentHiddenCount();
  els.hiddenItemCount.textContent = compact(storedCount);
  els.resetHiddenItemsButton.hidden = storedCount === 0;
  els.resetHiddenItemsButton.title = activeCount === storedCount
    ? `${compact(activeCount)} hidden in this ranking`
    : `${compact(activeCount)} hidden in this ranking; ${compact(storedCount)} saved on this device`;
}

function resetHiddenItems() {
  if (!state.hiddenItemKeys.size) return;
  state.hiddenItemKeys.clear();
  const persisted = saveHiddenItems();
  renderHiddenItemsControl();
  renderRankingSummary();
  renderRecommendations();
  toast(
    persisted
      ? "Hidden item list reset."
      : "Hidden item list reset for this session only.",
    persisted ? "success" : "error",
  );
}

function hideItem(rank) {
  const item = state.recommendations.find(
    (candidate) => candidate.rank === rank,
  );
  if (!item?.itemKey) return;
  state.hiddenItemKeys.add(item.itemKey);
  const persisted = saveHiddenItems();
  renderHiddenItemsControl();
  renderRankingSummary();
  renderRecommendations();
  toast(
    persisted
      ? `${item.displayName} hidden on this device.`
      : `${item.displayName} hidden for this session only.`,
    persisted ? "success" : "error",
  );
  els.recommendationList.querySelector("[data-open-detail]")?.focus();
}

function recommendationVariantLabel(category, identity) {
  if (category === "SkillGem") {
    const parts = [];
    if (identity.gemLevel != null) parts.push(`Level ${identity.gemLevel}`);
    if (identity.gemQuality != null) parts.push(`${identity.gemQuality}% quality`);
    if (identity.corrupted === true) parts.push("corrupted");
    if (!parts.length && identity.variant) parts.push(`Variant ${identity.variant}`);
    return parts.join(" · ");
  }
  if (category === "BaseType" || category === "ClusterJewel") {
    const parts = [];
    if (identity.itemLevel != null) {
      parts.push(`Item level ${identity.itemLevel}`);
    }
    if (identity.variant) parts.push(String(identity.variant));
    return parts.join(" · ");
  }
  const parts = [];
  if (identity.itemLevel != null) {
    parts.push(`Item level ${identity.itemLevel}`);
  }
  if (identity.mapTier != null) parts.push(`Tier ${identity.mapTier}`);
  if (
    identity.variant
    && !String(identity.variant).toLowerCase().includes(
      String(category).toLowerCase(),
    )
  ) {
    parts.push(String(identity.variant));
  }
  if (identity.links != null) parts.push(`${identity.links}-link`);
  if (identity.corrupted === true) parts.push("corrupted");
  return parts.join(" · ");
}

function recommendationDisplayName(name, category, identity, variantLabel) {
  const baseName = String(name || "Unknown item").trim();
  if (category === "ForbiddenJewel") {
    const jewelKind = /^Forbidden (?:Flame|Flesh)$/i.test(
      String(identity.variant || "").trim(),
    )
      ? String(identity.variant).trim()
      : String(baseName).match(/^Forbidden (?:Flame|Flesh)/i)?.[0] || "";
    const passiveName = String(identity.passiveName || "").trim();
    if (jewelKind && passiveName) {
      const lowerName = baseName.toLowerCase();
      if (
        lowerName.includes(jewelKind.toLowerCase())
        && lowerName.includes(passiveName.toLowerCase())
      ) {
        return baseName;
      }
      return `${jewelKind} (${passiveName})`;
    }
    return baseName;
  }
  const label = String(variantLabel || "").trim();
  if (!label || baseName.toLowerCase().includes(label.toLowerCase())) {
    return baseName;
  }
  return `${baseName} — ${label}`;
}

function officialTradeSearch(item) {
  const league = state.payload?.league?.id
    || state.status?.league?.id
    || state.status?.league?.name
    || "Standard";
  const query = {
    status: { option: "online" },
    stats: [{ type: "and", filters: [] }],
  };
  const identity = item.tradeIdentity || {};
  let broad = false;

  if (item.category === "ForbiddenJewel") {
    const jewelName = identity.variant
      || String(item.name).match(/^Forbidden (?:Flame|Flesh)/)?.[0]
      || item.name;
    query.name = jewelName;
    if (identity.baseType) query.type = identity.baseType;
    broad = Boolean(identity.passiveName);
  } else if (item.category === "SkillGem") {
    query.type = item.name;
    const miscFilters = {};
    if (identity.gemLevel != null) {
      miscFilters.gem_level = {
        min: identity.gemLevel,
        max: identity.gemLevel,
      };
    }
    const inferredGemQuality = identity.gemQuality != null
      ? identity.gemQuality
      : /^\d+c?$/i.test(String(identity.variant || ""))
        ? 0
        : null;
    if (inferredGemQuality != null) {
      miscFilters.quality = {
        min: inferredGemQuality,
        max: inferredGemQuality,
      };
    }
    if (identity.variant || identity.corrupted != null) {
      const variantSuggestsCorrupted = /c$/i.test(
        String(identity.variant || ""),
      );
      miscFilters.corrupted = {
        option: identity.corrupted === true || variantSuggestsCorrupted
          ? "true"
          : "false",
      };
    }
    if (Object.keys(miscFilters).length) {
      query.filters = {
        misc_filters: { filters: miscFilters },
      };
    }
  } else if (
    UNIQUE_TRADE_CATEGORIES.has(item.category)
    || item.category.startsWith("Unique")
  ) {
    query.name = item.name;
    if (/^Voices$/i.test(String(item.name || ""))) {
      const passiveCount = Number.parseInt(
        String(identity.variant || "").match(/^([13])\s+passives?$/i)?.[1]
        || "",
        10,
      );
      if (passiveCount === 1 || passiveCount === 3) {
        query.stats[0].filters.push({
          id: VOICES_PASSIVE_STAT_ID,
          value: { min: passiveCount, max: passiveCount },
          disabled: false,
        });
      } else {
        broad = true;
      }
    } else if (/^The Adorned$/i.test(String(item.name || ""))) {
      query.stats[0].filters.push({
        id: ADORNED_EFFECT_STAT_ID,
        value: { min: ADORNED_HIGH_ROLL_MINIMUM },
        disabled: false,
      });
    }
  } else {
    query.type = item.name;
  }

  const payload = {
    query,
    sort: { price: "asc" },
  };
  return {
    url: `https://www.pathofexile.com/trade/search/${encodeURIComponent(league)}?q=${encodeURIComponent(JSON.stringify(payload))}`,
    broad,
  };
}

function canonicalBroadLeague(value) {
  const raw = value && typeof value === "object"
    ? value.league || value.league_id || value.name || value.id
    : value;
  return BROAD_LEAGUE_ALIASES.get(String(raw || "").trim().toLowerCase()) || null;
}

function firstField(object, names) {
  for (const name of names) {
    if (object?.[name] != null && object[name] !== "") return object[name];
  }
  return null;
}

function normalizeForecast(item, days) {
  const horizonMap = item.forecast_horizons
    || item.forecastHorizons
    || item.horizons
    || item.forecasts
    || {};
  const nested = horizonMap[String(days)]
    || horizonMap[`${days}d`]
    || horizonMap[`${days}D`]
    || item[`forecast_${days}d`]
    || item[`forecast_${days}D`]
    || {};
  const suffixes = [`${days}d`, String(days)];
  const flatValue = (stem) => firstField(
    item,
    suffixes.flatMap((suffix) => [
      `${stem}_${suffix}_pct`,
      `${stem}_${suffix}`,
      `${stem}_${suffix}_divine`,
    ]),
  );
  const selectedHorizon = toNumber(state.payload?.horizon, 7);
  const selectedFallback = selectedHorizon === days;
  const expectedGainPct = nullableNumber(
    nested.expected_gain_pct,
    nested.expectedGainPct,
    flatValue("expected_gain"),
    selectedFallback ? item.expected_return_pct : null,
    selectedFallback ? item.expected_return : null,
  );
  const expectedPriceDivine = nullableNumber(
    nested.expected_price_divine,
    nested.expectedPriceDivine,
    firstField(item, [
      `expected_price_${days}d_divine`,
      `expected_price_${days}_divine`,
      `expected_price_divine_${days}d`,
    ]),
  );
  const historicalTargetDivine = nullableNumber(
    nested.historical_target_divine,
    nested.historical_target_price_divine,
    nested.historicalTargetDivine,
    nested.historicalTargetPriceDivine,
    firstField(item, [
      `historical_target_${days}d_divine`,
      `historical_target_${days}_divine`,
      `historical_target_divine_${days}d`,
      `historical_target_price_${days}d_divine`,
      `historical_target_price_divine_${days}d`,
    ]),
  );
  const rawHistoricalTargetDivine = nullableNumber(
    nested.raw_historical_target_divine,
    nested.rawHistoricalTargetDivine,
  );
  const metaMultiplier = nullableNumber(
    nested.meta_multiplier,
    nested.metaMultiplier,
    item.meta_multiplier,
    item.metaMultiplier,
  ) ?? 1;
  const historicalTargetGainPct = nullableNumber(
    nested.historical_target_gain_pct,
    nested.historicalTargetGainPct,
    flatValue("historical_target_gain"),
    selectedFallback ? item.historical_forward_return_pct : null,
  );
  const currentCurveGainPct = nullableNumber(
    nested.current_curve_gain_pct,
    nested.currentCurveGainPct,
    nested.current_curve_projection?.capped_gain_pct,
    nested.currentCurveProjection?.cappedGainPct,
    flatValue("current_curve_gain"),
    flatValue("curve_gain"),
    selectedFallback ? item.current_curve_projection_gain_pct : null,
  );
  const rawSamples = firstField(nested, [
    "sample_leagues",
    "sampleLeagues",
    "historical_sample_leagues",
    "historicalSampleLeagues",
  ]) ?? firstField(item, [
    `sample_leagues_${days}d`,
    `sample_leagues_${days}`,
    `forecast_sample_leagues_${days}d`,
    `historical_sample_leagues_${days}d`,
  ]);
  const rawLeagueNames = firstField(nested, [
    "sample_league_names",
    "sampleLeagueNames",
    "historical_leagues",
    "historicalLeagues",
    "leagues",
  ]);
  const leagueNames = (Array.isArray(rawLeagueNames)
    ? rawLeagueNames
    : Array.isArray(rawSamples)
      ? rawSamples
      : [])
    .map(canonicalBroadLeague)
    .filter(Boolean)
    .filter((league, index, leagues) => leagues.indexOf(league) === index);
  const sampleLeagues = leagueNames.length
    ? leagueNames.length
    : Array.isArray(rawSamples)
      ? 0
      : nullableNumber(rawSamples);
  const missingReason = firstField(nested, [
    "missing_reason",
    "missingReason",
    "reason",
  ]) || "no exact broad-league future-day price";
  const blend = nested.blend || {};
  const currentCurveUsed = Boolean(
    blend.current_curve_used
    ?? blend.currentCurveUsed
    ?? nested.current_curve_used
    ?? nested.currentCurveUsed,
  );
  return {
    days,
    expectedGainPct,
    expectedPriceDivine,
    historicalTargetDivine,
    rawHistoricalTargetDivine,
    metaMultiplier,
    historicalTargetGainPct,
    currentCurveGainPct,
    sampleLeagues,
    leagueNames,
    missingReason,
    currentCurveUsed,
  };
}

function normalizeRecommendation(item, index) {
  const leagueWeights = Array.isArray(item.seasonal_league_weights)
    ? item.seasonal_league_weights
    : [];
  const name = item.name || item.item_name || "Unknown item";
  const category = item.category || item.type || "Other";
  const rawTradeIdentity = item.trade_identity || item.tradeIdentity || {};
  const rawCorrupted = rawTradeIdentity.corrupted;
  const tradeIdentity = {
    variant: rawTradeIdentity.variant || null,
    baseType: rawTradeIdentity.base_type || rawTradeIdentity.baseType || null,
    gemLevel: nullableNumber(
      rawTradeIdentity.gem_level,
      rawTradeIdentity.gemLevel,
    ),
    gemQuality: nullableNumber(
      rawTradeIdentity.gem_quality,
      rawTradeIdentity.gemQuality,
    ),
    itemLevel: nullableNumber(
      rawTradeIdentity.item_level,
      rawTradeIdentity.itemLevel,
    ),
    links: nullableNumber(rawTradeIdentity.links),
    mapTier: nullableNumber(
      rawTradeIdentity.map_tier,
      rawTradeIdentity.mapTier,
    ),
    corrupted: rawCorrupted == null
      ? null
      : rawCorrupted === true || String(rawCorrupted).toLowerCase() === "true",
    passiveName: (
      rawTradeIdentity.passive_name
      || rawTradeIdentity.passiveName
      || null
    ),
  };
  const variantLabel = recommendationVariantLabel(category, tradeIdentity);
  const displayName = recommendationDisplayName(
    name,
    category,
    tradeIdentity,
    variantLabel,
  );
  return {
    ...item,
    rank: toNumber(item.rank, index + 1),
    itemKey: String(item.key || item.item_key || item.curve_key || ""),
    name,
    category,
    tradeIdentity,
    variantLabel,
    displayName,
    marketScopeCode: item.market_scope_code || item.marketScopeCode || null,
    marketScopeLabel: item.market_scope_label || item.marketScopeLabel || null,
    marketScopeCaveat: (
      item.market_scope_caveat || item.marketScopeCaveat || null
    ),
    searchText: String(
      item.search_text
      || `${displayName} ${category} ${tradeIdentity.passiveName || ""} ${item.market_scope_label || ""}`,
    ).toLowerCase(),
    priceDivine: nullableNumber(
      item.price_divine,
      item.current_price_divine,
      item.price,
    ),
    priceChaos: nullableNumber(item.price_chaos, item.current_price_chaos),
    forecasts: Object.fromEntries(
      FORECAST_HORIZONS.map((days) => [days, normalizeForecast(item, days)]),
    ),
    historicalAverage: item.historical_average_divine == null
      ? null
      : toNumber(item.historical_average_divine),
    historicalRecencyWeighted: nullableNumber(
      item.historical_recency_weighted_divine,
      item.recency_weighted_average_divine,
      item.weighted_historical_average_divine,
    ),
    historicalLevelLeagues: toNumber(
      item.historical_level_sample_leagues,
      leagueWeights.length,
    ),
    seasonalLeagueWeights: leagueWeights
      .map((entry, entryIndex) => ({
        league: entry.league || entry.league_id || `League ${entryIndex + 1}`,
        entryDivine: nullableNumber(entry.entry_divine, entry.price_divine),
        normalizedWeight: nullableNumber(entry.normalized_weight, entry.weight),
        ageRank: nullableNumber(entry.age_rank),
      }))
      .filter((entry) => entry.normalizedWeight != null),
  };
}

function rankStaticRecommendations(items, horizon) {
  if (runtime.mode !== "static") {
    return items.sort((a, b) => a.rank - b.rank);
  }
  for (const [index, item] of items.entries()) {
    item.rank = toNumber(
      item.static_ranks?.[String(horizon)]
        ?? item.staticRanks?.[String(horizon)],
      index + 1,
    );
  }
  return items.sort((a, b) => a.rank - b.rank);
}

function renderRankingSummary() {
  const payload = state.payload || {};
  const rankingSummary = payload.ranking_summary || {};
  const investmentScope = payload.investment_scope || {};
  const excludedItems = toNumber(
    investmentScope.excluded_item_count,
    investmentScope.excluded_category_items,
  );
  const returned = toNumber(rankingSummary.returned, state.recommendations.length);
  const horizon = toNumber(payload.horizon, toNumber(els.horizonSelect.value, 7));
  const hiddenCount = currentHiddenCount();
  const excludedCategoryCount = activeExcludedCategoryCount();
  const filtersActive = excludedCategoryCount > 0
    || state.priceRange !== "all"
    || Boolean(state.query.trim())
    || hiddenCount > 0;
  const visible = filteredRecommendations().length;
  const showing = filtersActive
    ? `${compact(visible)} of ${compact(returned)} ranked item variants are visible`
    : `${compact(returned)} ranked item variants across all pages`;
  const hiddenNote = hiddenCount
    ? ` ${compact(hiddenCount)} hidden on this device.`
    : "";
  const categoryNote = excludedCategoryCount
    ? ` ${compact(excludedCategoryCount)} item ${excludedCategoryCount === 1 ? "section is" : "sections are"} excluded on this device.`
    : "";
  els.modelNote.textContent = returned
    ? `${showing}, sorted by gross ${horizon}-day expected gain.${hiddenNote}${categoryNote} Forecasts use poe.ninja Medium/High observations from Mirage, Keepers, Mercenaries, and Settlers; comparison charts also show exact Low-confidence rows as non-forecast context. Forecast curves use one exact point per UTC league day; optional GGG hourly audit rows never enter them. Base types other than Simplex Amulet and Focused Amulet, unique items (except 1-/3-passive Voices and roll-unresolved Sublime Vision, The Adorned, and Watcher's Eye markets), Valdo maps, and non-Awakened gems are archived but omitted from this investment list.${excludedItems ? ` ${compact(excludedItems)} archived markets are omitted by category, the 1c floor, or the persistent-decline rule.` : ""}`
    : `No item forecast is available yet. Build the broad-league archive, then sync again.`;
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = String(value ?? "");
  return node.innerHTML;
}

function normalizeLeagueCurve(points) {
  if (!Array.isArray(points)) return [];
  return points
    .map((point) => {
      const leagueDay = nullableNumber(point?.league_day, point?.day);
      const divineValue = nullableNumber(
        point?.divine_value,
        point?.price_divine,
        point?.value,
      );
      const sampleLeagues = nullableNumber(
        point?.contributing_leagues,
        point?.sample_leagues,
        point?.samples,
        point?.league_count,
      );
      const forecastGradeSampleLeagues = nullableNumber(
        point?.forecast_grade_contributing_leagues,
        point?.forecastGradeContributingLeagues,
      );
      const confidenceValue = nullableNumber(point?.confidence);
      const rawModelGrade = point?.model_grade ?? point?.modelGrade;
      return {
        leagueDay,
        divineValue,
        sampleLeagues,
        forecastGradeSampleLeagues,
        confidence: confidenceValue,
        modelGrade: rawModelGrade == null ? true : rawModelGrade === true,
        observedAt: point?.observed_at || point?.observedAt || null,
        source: point?.source || null,
      };
    })
    .filter((point) => (
      point.leagueDay != null
      && point.divineValue != null
      && point.divineValue >= 0
    ))
    .sort((left, right) => left.leagueDay - right.leagueDay);
}

function chartNumber(value) {
  const number = toNumber(value);
  if (number >= 100) return number.toFixed(0);
  if (number >= 10) return number.toFixed(1);
  if (number >= 1) return number.toFixed(2);
  return number.toPrecision(2);
}

function seasonalComparisonMarkup(rawComparison, itemName) {
  const comparison = rawComparison || {};
  const currentLeague = comparison.current_league
    || comparison.currentLeague
    || {};
  const weightedHistorical = comparison.weighted_historical
    || comparison.weightedHistorical
    || {};
  const calculation = comparison.calculation || {};
  const currentCoverage = currentLeague.coverage
    || comparison.current_coverage
    || comparison.currentCoverage
    || {};
  const currentCurve = normalizeLeagueCurve(
    currentLeague.points
      || comparison.current_curve
      || comparison.currentCurve,
  );
  const fullHistoricalCurve = normalizeLeagueCurve(
    weightedHistorical.points
      || comparison.weighted_historical_curve
      || comparison.weightedHistoricalCurve,
  );
  const chartHorizonDays = Math.max(...FORECAST_HORIZONS);
  const currentLastDay = currentCurve.length
    ? Math.max(...currentCurve.map((point) => point.leagueDay))
    : null;
  const chartDayCeiling = currentLastDay == null
    ? null
    : currentLastDay + chartHorizonDays;
  const historicalCurve = chartDayCeiling == null
    ? fullHistoricalCurve
    : fullHistoricalCurve.filter(
      (point) => point.leagueDay <= chartDayCeiling,
    );
  const omittedHistoricalPoints = nullableNumber(
    weightedHistorical.omitted_points,
    weightedHistorical.omittedPoints,
  ) ?? (fullHistoricalCurve.length - historicalCurve.length);

  if (!currentCurve.length && !historicalCurve.length) {
    return `
      <section class="seasonal-comparison seasonal-comparison-empty" aria-label="League price curve comparison">
        <div class="comparison-heading">
          <div>
            <span class="comparison-kicker">PRICE CURVE</span>
            <h3>Current league vs broad-league history</h3>
          </div>
        </div>
        <p>No comparable league-day curve is stored for this item yet. The chart will appear after matching current- and past-league observations have been archived.</p>
      </section>`;
  }

  const allPoints = [...currentCurve, ...historicalCurve];
  // League charts always begin at day 1. An absent item observation stays an
  // honest gap; the chart never invents a price or connects it to day 1.
  const dayMin = 1;
  const dayMax = Math.max(...allPoints.map((point) => point.leagueDay));
  const rawValueMin = Math.min(...allPoints.map((point) => point.divineValue));
  const rawValueMax = Math.max(...allPoints.map((point) => point.divineValue));
  const valueSpan = rawValueMax - rawValueMin;
  const valuePadding = valueSpan > 0
    ? valueSpan * 0.12
    : Math.max(rawValueMax * 0.12, 0.1);
  const valueMin = Math.max(0, rawValueMin - valuePadding);
  const valueMax = rawValueMax + valuePadding;
  const daySpan = dayMax - dayMin || 1;
  const valueRange = valueMax - valueMin || 1;

  const width = 760;
  const height = 330;
  const plot = { left: 68, right: 738, top: 22, bottom: 274 };
  const plotWidth = plot.right - plot.left;
  const plotHeight = plot.bottom - plot.top;
  const x = (day) => plot.left + ((day - dayMin) / daySpan) * plotWidth;
  const y = (value) => plot.bottom - ((value - valueMin) / valueRange) * plotHeight;
  const pointString = (points) => points
    .map((point) => `${x(point.leagueDay).toFixed(2)},${y(point.divineValue).toFixed(2)}`)
    .join(" ");
  const lineMarkup = (points, kind) => {
    const segments = [];
    let segment = [];
    points.forEach((point) => {
      if (
        segment.length
        && point.leagueDay - segment[segment.length - 1].leagueDay > 1
      ) {
        segments.push(segment);
        segment = [];
      }
      segment.push(point);
    });
    if (segment.length) segments.push(segment);
    return segments
      .filter((pointsInSegment) => pointsInSegment.length > 1)
      .map((pointsInSegment) => (
        `<polyline class="comparison-line ${kind}" points="${pointString(pointsInSegment)}"></polyline>`
      ))
      .join("");
  };

  const xTickCount = Math.min(5, Math.max(2, Math.floor(daySpan) + 1));
  const xTicks = Array.from({ length: xTickCount }, (_, index) => (
    dayMin + (daySpan * index) / (xTickCount - 1)
  ));
  const yTicks = Array.from({ length: 5 }, (_, index) => (
    valueMin + ((valueMax - valueMin) * index) / 4
  ));
  const chartId = `seasonal-chart-${Math.abs(
    [...String(itemName)].reduce((total, character) => (
      ((total << 5) - total + character.charCodeAt(0)) | 0
    ), 0),
  )}`;
  const titleId = `${chartId}-title`;
  const descriptionId = `${chartId}-description`;

  const gridMarkup = yTicks
    .map((tick) => `
      <g class="comparison-grid">
        <line x1="${plot.left}" y1="${y(tick).toFixed(2)}" x2="${plot.right}" y2="${y(tick).toFixed(2)}"></line>
        <text x="${plot.left - 11}" y="${(y(tick) + 4).toFixed(2)}" text-anchor="end">${escapeHtml(chartNumber(tick))}</text>
      </g>`)
    .join("");
  const xAxisMarkup = xTicks
    .map((tick) => `
      <g class="comparison-axis-tick">
        <line x1="${x(tick).toFixed(2)}" y1="${plot.bottom}" x2="${x(tick).toFixed(2)}" y2="${plot.bottom + 5}"></line>
        <text x="${x(tick).toFixed(2)}" y="${plot.bottom + 22}" text-anchor="middle">Day ${Math.round(tick)}</text>
      </g>`)
    .join("");
  const pointMarkup = (points, kind, label) => points
    .map((point) => {
      const sampleText = point.sampleLeagues == null
        ? ""
        : `, ${point.sampleLeagues} broad league${point.sampleLeagues === 1 ? "" : "s"}`;
      const forecastGradeText = point.forecastGradeSampleLeagues == null
        ? ""
        : `, ${point.forecastGradeSampleLeagues} forecast-grade`;
      const confidenceText = point.confidence == null
        ? ""
        : `, source confidence ${(point.confidence * 100).toFixed(0)}%`;
      const accessibleLabel = `${label}, league day ${point.leagueDay}, ${money(point.divineValue, 2)}${sampleText}${forecastGradeText}${confidenceText}`;
      return `
        <circle
          class="comparison-point ${kind}"
          cx="${x(point.leagueDay).toFixed(2)}"
          cy="${y(point.divineValue).toFixed(2)}"
          r="3.5"
          tabindex="0"
          aria-label="${escapeHtml(accessibleLabel)}"
        ><title>${escapeHtml(accessibleLabel)}</title></circle>`;
    })
    .join("");

  const sampleCounts = historicalCurve
    .map((point) => point.sampleLeagues)
    .filter((value) => value != null);
  const sampleMin = sampleCounts.length ? Math.min(...sampleCounts) : null;
  const sampleMax = sampleCounts.length ? Math.max(...sampleCounts) : null;
  const sampleLabel = sampleMin == null
    ? "sample count unavailable"
    : sampleMin === sampleMax
      ? `${sampleMin} broad league${sampleMin === 1 ? "" : "s"} per point`
      : `${sampleMin}–${sampleMax} broad leagues per point`;
  const recencyDecay = nullableNumber(
    calculation.recency_decay_per_league,
    calculation.recencyDecayPerLeague,
    comparison.recency_decay_per_league,
    comparison.recencyDecayPerLeague,
    state.payload?.seasonal_model?.recency_decay_per_league,
  );
  const currentLeagueName = currentLeague.league_name
    || currentLeague.leagueName
    || "Current league";
  const weightExplanation = recencyDecay == null
    ? "Only Mirage, Keepers, Mercenaries, and Settlers contribute; newer broad leagues receive more weight and available weights are normalized to 100%."
    : `Only Mirage, Keepers, Mercenaries, and Settlers contribute. The newest starts at full weight and each step older receives ${(recencyDecay * 100).toFixed(0)}% of the next newer league's weight; available weights are normalized to 100%.`;
  const windowExplanation = omittedHistoricalPoints > 0
    ? `The plot ends at league day ${chartDayCeiling}, matching the current phase plus the longest ${chartHorizonDays}-day forecast window; ${omittedHistoricalPoints} later archived point${omittedHistoricalPoints === 1 ? " is" : "s are"} omitted from this near-term view.`
    : "";
  const currentFirstDay = currentCurve.length
    ? Math.min(...currentCurve.map((point) => point.leagueDay))
    : null;
  const openingGapExplanation = currentCoverage.source_limitation
    || currentCoverage.sourceLimitation
    || (currentFirstDay != null && currentFirstDay > 1
      ? `No source trade was recorded for this exact item before league day ${currentFirstDay}; days 1–${currentFirstDay - 1} remain blank rather than estimated.`
      : "");
  const missingCurrentDays = Array.isArray(currentCoverage.missing_days)
    ? currentCoverage.missing_days
    : Array.isArray(currentCoverage.missingDays)
      ? currentCoverage.missingDays
      : [];
  const archiveSource = currentCoverage.dated_archive_source
    || currentCoverage.datedArchiveSource
    || null;
  const coverageExplanation = archiveSource
    ? `Current dated archive: ${archiveSource}; ${currentCurve.length} stored day${currentCurve.length === 1 ? "" : "s"}${missingCurrentDays.length ? `, with day${missingCurrentDays.length === 1 ? "" : "s"} ${missingCurrentDays.join(", ")} left blank` : ""}.`
    : "";
  const chartDescription = `${itemName} has ${currentCurve.length} current-league observations and ${historicalCurve.length} recency-weighted historical observations from league day ${dayMin} through ${dayMax}; ${sampleLabel}. ${openingGapExplanation}`;

  const currentByDay = new Map(
    currentCurve.map((point) => [point.leagueDay, point]),
  );
  const historicalByDay = new Map(
    historicalCurve.map((point) => [point.leagueDay, point]),
  );
  const exactDays = [...new Set([
    1,
    ...currentCurve.map((point) => point.leagueDay),
    ...historicalCurve.map((point) => point.leagueDay),
  ])].sort((left, right) => left - right);
  const tableRows = exactDays
    .map((day) => {
      const current = currentByDay.get(day);
      const historical = historicalByDay.get(day);
      return `
        <tr>
          <th scope="row">Day ${escapeHtml(day)}</th>
          <td>${current ? money(current.divineValue, 2) : "—"}</td>
          <td>${historical ? money(historical.divineValue, 2) : "—"}</td>
          <td>${historical?.sampleLeagues == null ? "—" : `${historical.sampleLeagues}${historical.forecastGradeSampleLeagues == null ? "" : ` (${historical.forecastGradeSampleLeagues} forecast-grade)`}`}</td>
        </tr>`;
    })
    .join("");

  return `
    <section class="seasonal-comparison" aria-labelledby="${titleId}">
      <div class="comparison-heading">
        <div>
          <span class="comparison-kicker">PRICE CURVE</span>
          <h3 id="${titleId}">Current league vs broad-league history</h3>
        </div>
        <div class="comparison-legend" aria-label="Chart legend">
          <span><i class="current"></i>${escapeHtml(currentLeagueName)}</span>
          <span><i class="historical"></i>Weighted broad leagues</span>
        </div>
      </div>
      <div class="comparison-chart-shell">
        <svg
          class="comparison-chart"
          viewBox="0 0 ${width} ${height}"
          role="img"
          aria-labelledby="${titleId} ${descriptionId}"
        >
          <desc id="${descriptionId}">${escapeHtml(chartDescription)} Lines only connect stored observations; missing days are not filled.</desc>
          ${gridMarkup}
          <line class="comparison-axis" x1="${plot.left}" y1="${plot.bottom}" x2="${plot.right}" y2="${plot.bottom}"></line>
          ${xAxisMarkup}
          <text class="comparison-axis-label" x="17" y="${(plot.top + plot.bottom) / 2}" text-anchor="middle" transform="rotate(-90 17 ${(plot.top + plot.bottom) / 2})">Divine Orbs</text>
          ${lineMarkup(historicalCurve, "historical")}
          ${lineMarkup(currentCurve, "current")}
          ${pointMarkup(historicalCurve, "historical", "Weighted broad leagues")}
          ${pointMarkup(currentCurve, "current", currentLeagueName)}
        </svg>
      </div>
      <p class="comparison-method">${escapeHtml(openingGapExplanation)} ${escapeHtml(coverageExplanation)} ${escapeHtml(weightExplanation)} ${escapeHtml(
        `${sampleLabel}.`,
      )} All positive exact poe.ninja rows are plotted, including Low-confidence context; only Medium/High contributors can set a forecast target. ${escapeHtml(windowExplanation)} Hover or focus a point for its exact value; the chart does not synthesize missing observations.</p>
      <details class="comparison-data">
        <summary>Show exact plotted values</summary>
        <div>
          <table>
            <thead>
              <tr><th>League day</th><th>Current</th><th>Weighted broad leagues</th><th>Broad samples</th></tr>
            </thead>
            <tbody>${tableRows}</tbody>
          </table>
        </div>
      </details>
    </section>`;
}

async function loadSeasonalComparison(item) {
  const container = els.detailContent.querySelector("[data-seasonal-comparison]");
  if (!container) return;
  const itemKey = item.curve_key
    || item.curveKey
    || item.item_key
    || item.itemKey
    || item.key;
  if (!itemKey) {
    container.innerHTML = seasonalComparisonMarkup(null, item.displayName);
    return;
  }

  try {
    const payload = await api(`/api/history?key=${encodeURIComponent(itemKey)}`);
    if (!container.isConnected) return;
    container.outerHTML = seasonalComparisonMarkup(
      payload?.seasonal_comparison || payload?.seasonalComparison,
      item.displayName,
    );
  } catch (error) {
    if (!container.isConnected) return;
    container.innerHTML = `
      <section class="seasonal-comparison seasonal-comparison-empty" aria-label="League price curve comparison">
        <div class="comparison-heading">
          <div>
            <span class="comparison-kicker">PRICE CURVE</span>
            <h3>Current league vs past leagues</h3>
          </div>
        </div>
        <p>The locally archived curve could not be loaded: ${escapeHtml(error.message)}</p>
      </section>`;
  }
}

function filteredRecommendations() {
  const query = state.query.trim().toLowerCase();
  const selectedThreshold = PRICE_THRESHOLDS[state.priceRange] || null;
  return state.recommendations.filter((item) => {
    const categoryMatches = !state.excludedCategories.has(item.category);
    const queryMatches = !query || item.searchText.includes(query);
    const thresholdPrice = selectedThreshold
      ? Number(item[selectedThreshold.field])
      : null;
    const priceMatches = !selectedThreshold || (
      Number.isFinite(thresholdPrice)
      && thresholdPrice >= selectedThreshold.minimum
    );
    const hiddenMatches = !item.itemKey || !state.hiddenItemKeys.has(item.itemKey);
    return categoryMatches && priceMatches && queryMatches && hiddenMatches;
  });
}

function paginatedRecommendations(items) {
  const pageSize = Math.max(1, toNumber(state.pageSize, 50));
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  state.page = Math.min(Math.max(1, toNumber(state.page, 1)), totalPages);
  const startIndex = (state.page - 1) * pageSize;
  const endIndex = Math.min(startIndex + pageSize, items.length);
  return {
    items: items.slice(startIndex, endIndex),
    total: items.length,
    totalPages,
    start: items.length ? startIndex + 1 : 0,
    end: endIndex,
  };
}

function renderPagination(pagination) {
  if (!els.paginationControls) return;
  els.paginationControls.hidden = pagination.total === 0;
  if (!pagination.total) return;
  els.paginationSummary.textContent = (
    `Page ${compact(state.page)} of ${compact(pagination.totalPages)}`
    + ` · ${compact(pagination.start)}–${compact(pagination.end)}`
    + ` of ${compact(pagination.total)} visible`
  );
  els.previousPageButton.disabled = state.page <= 1;
  els.nextPageButton.disabled = state.page >= pagination.totalPages;
  els.pageSizeSelect.value = String(state.pageSize);
}

function forecastPercent(value, digits = 1) {
  if (value == null) return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number >= 0 ? "+" : ""}${number.toFixed(digits)}%`;
}

function forecastCellMarkup(forecast, days, selectedHorizon) {
  const selected = days === selectedHorizon;
  const expected = forecast?.expectedGainPct;
  const tone = expected == null
    ? "missing"
    : expected < 0
      ? "negative"
      : "positive";
  const target = forecast?.expectedPriceDivine
    ?? forecast?.historicalTargetDivine;
  return `
    <span class="forecast-cell horizon-${days} ${selected ? "selected-horizon" : ""} ${tone}">
      <strong>${forecastPercent(expected)}</strong>
      <span class="cell-meta">${expected == null
        ? "no broad future-day price"
        : target == null
          ? `${days}-day gross`
          : `expected ${money(target, 2)}`}</span>
    </span>`;
}

function renderRecommendations() {
  const filteredItems = filteredRecommendations();
  const pagination = paginatedRecommendations(filteredItems);
  const items = pagination.items;
  const horizon = toNumber(state.payload?.horizon, toNumber(els.horizonSelect.value, 7));
  renderHiddenItemsControl();
  renderPagination(pagination);
  els.forecastHeaders.forEach((header) => {
    const selected = toNumber(header.dataset.forecastHeader) === horizon;
    header.classList.toggle("selected-horizon", selected);
    header.textContent = `EXPECTED ${header.dataset.forecastHeader}D${selected ? " · SORT" : ""}`;
  });
  if (
    runtime.mode === "static"
    && runtime.manifest?.schema_version >= 2
    && items.length
  ) {
    const signature = `${horizon}:${state.page}:${state.pageSize}:${items
      .map((item) => item.itemKey)
      .join("|")}`;
    if (runtime.pageRenderSignature !== `ready:${signature}`) {
      if (runtime.pageRenderSignature === `loading:${signature}`) return;
      runtime.pageRenderSignature = `loading:${signature}`;
      const sequence = ++runtime.pageRenderSequence;
      els.recommendationList.innerHTML = `
        <div class="loading-state">
          <span class="loading-rune" aria-hidden="true"></span>
          <strong>Loading this ranking page</strong>
          <p>Fetching only the visible forecast rows from the published snapshot.</p>
        </div>`;
      loadStaticPageDetails(items, horizon)
        .then(() => {
          if (sequence !== runtime.pageRenderSequence) return;
          runtime.pageRenderSignature = `ready:${signature}`;
          renderRecommendations();
        })
        .catch((error) => {
          if (sequence !== runtime.pageRenderSequence) return;
          runtime.pageRenderSignature = `error:${signature}`;
          els.recommendationList.innerHTML = `
            <div class="empty-state">
              <strong>This ranking page could not be loaded</strong>
              <p>${escapeHtml(error.message)}</p>
            </div>`;
        });
      return;
    }
  }
  if (!state.recommendations.length) {
    els.recommendationList.innerHTML = `
      <div class="empty-state">
        <span class="summary-icon ideas" aria-hidden="true">✦</span>
        <strong>No forecasts yet</strong>
        <p>Run a market sync, then continue the past-league archive. Exact future-day prices from Mirage, Keepers, Mercenaries, and Settlers are required for a forecast; missing estimates stay blank.</p>
        <button class="secondary-button" type="button" data-sync>Sync market now</button>
      </div>`;
    els.recommendationList.querySelector("[data-sync]")?.addEventListener("click", syncMarket);
    return;
  }

  if (!items.length) {
    const resetAction = state.hiddenItemKeys.size
      ? '<button class="secondary-button" type="button" data-reset-hidden>Reset hidden items</button>'
      : "";
    const resetCategoryAction = activeExcludedCategoryCount()
      ? '<button class="secondary-button" type="button" data-reset-categories>Include all sections</button>'
      : "";
    els.recommendationList.innerHTML = `
      <div class="empty-state">
        <strong>No match for this filter</strong>
        <p>Adjust the minimum price, included sections, item search, or hidden list.</p>
        <div class="empty-state-actions">${resetCategoryAction}${resetAction}</div>
      </div>`;
    els.recommendationList.querySelector("[data-reset-hidden]")?.addEventListener(
      "click",
      resetHiddenItems,
    );
    els.recommendationList.querySelector("[data-reset-categories]")?.addEventListener(
      "click",
      includeAllCategories,
    );
    return;
  }

  els.recommendationList.innerHTML = items
    .map((item) => {
      const chaos = item.priceChaos ? `${compact(item.priceChaos)}c` : "divine-relative";
      const coverage = FORECAST_HORIZONS
        .map((days) => {
          const samples = item.forecasts[days]?.sampleLeagues;
          return `${days}D ${samples > 0 ? `${samples}/4` : "—"}`;
        })
        .join(" · ");
      const selectedForecast = item.forecasts[horizon];
      const trade = officialTradeSearch(item);
      const tradeTitle = trade.broad
        ? `Search official trade for ${item.tradeIdentity.variant || item.name}; choose ${item.tradeIdentity.passiveName} in the Allocates filter`
        : `Search official trade for ${item.displayName}`;
      const detailLabel = (
        `Open ${item.displayName} forecasts and current versus broad-league price curves; `
        + `${horizon}-day expected gain ${selectedForecast?.expectedGainPct == null
          ? "unavailable"
          : forecastPercent(selectedForecast.expectedGainPct)}`
      );
      return `
        <article class="recommendation-row" data-rank="${item.rank}">
          <div class="row-action-cell hide-cell">
            ${item.itemKey
              ? `<button class="hide-item-button" type="button" data-hide-rank="${item.rank}" aria-label="Hide ${escapeHtml(item.displayName)} from recommendations">Hide</button>`
              : ""}
          </div>
          <div class="item-cell">
            <span class="rank">#${item.rank}</span>
            <div class="item-summary">
              <button class="item-name-button" type="button" data-open-detail="${item.rank}" aria-label="${escapeHtml(detailLabel)}">
                <strong>${escapeHtml(item.displayName)}</strong>
              </button>
              <small>
                <span>${escapeHtml(item.category)}</span>
                ${item.marketScopeLabel
                  ? `<span class="cell-meta">${escapeHtml(item.marketScopeLabel)}</span>`
                  : ""}
              </small>
            </div>
          </div>
          <div class="market-cell">
            <strong>${item.priceDivine == null ? "—" : money(item.priceDivine, 2)}</strong>
            <span class="cell-meta">${escapeHtml(chaos)}</span>
          </div>
          ${FORECAST_HORIZONS.map((days) => (
            forecastCellMarkup(item.forecasts[days], days, horizon)
          )).join("")}
          <div class="coverage-cell">
            <strong>${escapeHtml(coverage)}</strong>
            <span class="cell-meta">exact future-day samples</span>
          </div>
          <div class="curve-cell">
            <button class="curve-button" type="button" data-open-detail="${item.rank}" aria-label="${escapeHtml(detailLabel)}">
              <strong>Compare</strong>
              <span class="cell-meta">current vs broad history →</span>
            </button>
          </div>
          <div class="row-action-cell trade-cell">
            <a class="trade-link" href="${escapeHtml(trade.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(tradeTitle)}">
              ${trade.broad ? "Trade (base)" : "Trade"} ↗
            </a>
          </div>
        </article>`;
    })
    .join("");

  els.recommendationList.querySelectorAll("[data-open-detail]").forEach((button) => {
    button.addEventListener("click", () => openDetail(toNumber(button.dataset.openDetail)));
  });
  els.recommendationList.querySelectorAll("[data-hide-rank]").forEach((button) => {
    button.addEventListener("click", () => hideItem(toNumber(button.dataset.hideRank)));
  });
}

function renderCategoryOptions() {
  state.availableCategories = [
    ...new Set(state.recommendations.map((item) => item.category).filter(Boolean)),
  ].sort((left, right) => categoryLabel(left).localeCompare(categoryLabel(right)));
  if (els.categoryChecklist) {
    els.categoryChecklist.innerHTML = state.availableCategories.length
      ? state.availableCategories.map((category, index) => `
          <label class="category-option" for="categoryOption${index}">
            <input
              id="categoryOption${index}"
              type="checkbox"
              data-category-index="${index}"
              ${state.excludedCategories.has(category) ? "" : "checked"}
            />
            <span>${escapeHtml(categoryLabel(category))}</span>
          </label>`).join("")
      : '<p class="category-filter-empty">No ranked sections yet.</p>';
  }
  renderCategoryFilterSummary();
}

function renderSources() {
  const sources = state.status?.sources || [];
  if (!sources.length) {
    els.sourceList.innerHTML = `
      <div class="source-row">
        <i class="warning"></i>
        <span><strong>Waiting for first status check</strong><small>Source health will appear after startup.</small></span>
      </div>`;
    return;
  }
  els.sourceList.innerHTML = sources
    .map((source) => {
      const raw = String(source.status || "unknown").toLowerCase();
      const kind = ["ok", "live", "healthy", "current"].includes(raw)
        ? "live"
        : ["error", "failed", "offline", "unavailable"].includes(raw)
          ? "error"
          : "warning";
      return `
        <div class="source-row">
          <i class="${kind}"></i>
          <span>
            <strong>${escapeHtml(source.name || source.source || "Data source")}</strong>
            <small>${escapeHtml(source.detail || source.message || source.status || "No detail")}</small>
          </span>
        </div>`;
    })
    .join("");
}

function forecastDetailMarkup(forecast, selectedHorizon) {
  const hasHistoricalSamples = forecast.sampleLeagues > 0;
  const leagueList = forecast.leagueNames.length
    ? forecast.leagueNames.join(", ")
    : hasHistoricalSamples
      ? `${forecast.sampleLeagues} of ${BROAD_LEAGUES.join(", ")}`
      : "no exact broad-league future-day price";
  const metaAdjusted = Math.abs((forecast.metaMultiplier || 1) - 1) > 1e-9;
  const targetLabel = metaAdjusted
    ? "META-ADJUSTED FUTURE TARGET"
    : "HISTORICAL FUTURE TARGET";
  const targetNote = forecast.historicalTargetGainPct == null
    ? "—"
    : metaAdjusted && forecast.rawHistoricalTargetDivine != null
      ? `${forecastPercent(forecast.historicalTargetGainPct)} from today; raw ${money(forecast.rawHistoricalTargetDivine, 2)} × ${forecast.metaMultiplier.toFixed(2)}`
      : `${forecastPercent(forecast.historicalTargetGainPct)} from today`;
  return `
    <section class="forecast-detail-card ${forecast.days === selectedHorizon ? "selected-horizon" : ""}">
      <header>
        <div>
          <span>${forecast.days}-DAY FORECAST</span>
          <strong>${forecastPercent(forecast.expectedGainPct)}</strong>
        </div>
        ${forecast.days === selectedHorizon ? "<em>RANKING HORIZON</em>" : ""}
      </header>
      <div class="forecast-components">
        <article>
          <span>GROSS EXPECTED GAIN</span>
          <strong>${forecastPercent(forecast.expectedGainPct)}</strong>
          <small>${forecast.currentCurveUsed ? "70/30 log-price blend" : "historical target only"}</small>
        </article>
        <article>
          <span>${targetLabel}</span>
          <strong>${forecast.historicalTargetDivine == null ? "—" : money(forecast.historicalTargetDivine, 2)}</strong>
          <small>${targetNote}</small>
        </article>
        <article>
          <span>CURRENT-CURVE TREND</span>
          <strong>${forecastPercent(forecast.currentCurveGainPct)}</strong>
          <small>${forecast.currentCurveUsed ? "robust projection, capped" : "not blended"}</small>
        </article>
        <article>
          <span>BROAD FUTURE-DAY SAMPLES</span>
          <strong>${hasHistoricalSamples ? `${forecast.sampleLeagues}/4` : "—"}</strong>
          <small>${escapeHtml(leagueList)}</small>
        </article>
      </div>
      ${forecast.expectedGainPct == null
        ? `<p class="forecast-missing">${escapeHtml(forecast.missingReason || "no exact broad-league future-day price")}</p>`
        : ""}
    </section>`;
}

function openDetail(rank) {
  const item = state.recommendations.find((candidate) => candidate.rank === rank);
  if (!item) return;
  const horizon = toNumber(state.payload?.horizon, toNumber(els.horizonSelect.value, 7));
  const selectedForecast = item.forecasts[horizon];
  els.detailContent.innerHTML = `
    <div class="detail-topline">
      <span class="detail-rank">#${item.rank}</span>
      <span class="detail-category">${escapeHtml(item.category)}</span>
    </div>
    <h2>${escapeHtml(item.displayName)}</h2>
    <p class="detail-summary">Gross price forecasts from four broadly covered leagues and the current-league curve. These estimates do not deduct trading costs or adjust for execution depth.</p>
    ${item.marketScopeCaveat
      ? `<p class="detail-caveat forecast-method-note"><strong>${escapeHtml(item.marketScopeLabel || "Market scope warning")}</strong>: ${escapeHtml(item.marketScopeCaveat)}</p>`
      : ""}
    <div class="detail-metrics">
      <div><span>CURRENT PRICE</span><strong>${item.priceDivine == null ? "—" : money(item.priceDivine, 2)}</strong></div>
      <div><span>${horizon}D EXPECTED GAIN</span><strong>${forecastPercent(selectedForecast?.expectedGainPct)}</strong></div>
      <div><span>${horizon}D EXPECTED PRICE</span><strong>${selectedForecast?.expectedPriceDivine == null ? "—" : money(selectedForecast.expectedPriceDivine, 2)}</strong></div>
      <div><span>${horizon}D RANK</span><strong>#${item.rank}</strong></div>
    </div>
    <div class="forecast-detail-grid">
      ${FORECAST_HORIZONS.map((days) => (
        forecastDetailMarkup(item.forecasts[days], horizon)
      )).join("")}
    </div>
    <div class="seasonal-comparison-loading" data-seasonal-comparison aria-live="polite">
      <span class="loading-rune" aria-hidden="true"></span>
      <p>Loading the current and weighted broad-league price curves…</p>
    </div>
    <p class="detail-caveat forecast-method-note">
      Historical targets use only Mirage, Keepers, Mercenaries, and Settlers.
      Only poe.ninja Medium/High observations enter forecasts. The comparison
      chart also plots exact Low-confidence rows as context, but they cannot set
      a forecast target.
      For Forbidden Jewels, a displayed meta-adjusted target multiplies that
      raw curve target by the current-versus-past ascendancy-share signal; the
      chart itself always shows the unadjusted poe.ninja observations.
      The expected price is a 70% historical-target / 30% capped current-trend
      blend in log-price space when the current curve is usable; otherwise the
      historical target stands alone. Missing estimates mean no exact
      broad-league future-day price—not a 0% return.
    </p>`;
  els.detailDialog.showModal();
  loadSeasonalComparison(item);
}

function renderPayload(raw) {
  const list = Array.isArray(raw?.rankings)
    ? raw.rankings
    : Array.isArray(raw?.recommendations)
      ? raw.recommendations
      : Array.isArray(raw?.items)
        ? raw.items
        : [];
  state.payload = { ...(raw || {}) };
  delete state.payload.rankings;
  delete state.payload.recommendations;
  delete state.payload.items;
  state.page = 1;
  if (
    runtime.mode === "static"
    && runtime.catalogRows === list
    && runtime.normalizedRecommendations
  ) {
    state.recommendations = runtime.normalizedRecommendations;
  } else {
    state.recommendations = list.map(normalizeRecommendation);
    if (runtime.mode === "static") {
      runtime.catalogRows = list;
      runtime.normalizedRecommendations = state.recommendations;
    }
  }
  rankStaticRecommendations(
    state.recommendations,
    toNumber(state.payload.horizon, 7),
  );
  renderCategoryOptions();
  renderHiddenItemsControl();
  renderRankingSummary();
  renderRecommendations();
}

async function loadStatus({ quiet = false } = {}) {
  try {
    state.status = normalizeStatus(await api("/api/status"));
    renderStatus();
  } catch (error) {
    if (!quiet) toast(`Could not read market status: ${error.message}`, "error");
    setSourceState(
      "error",
      runtime.mode === "static"
        ? "The published market snapshot could not be loaded. Reload the page or inspect the latest update workflow."
        : "The local service is not responding. Start the app, then reload this page.",
    );
    els.leagueName.textContent = runtime.mode === "static"
      ? "Published data unavailable"
      : "Service offline";
    els.leagueDay.textContent = "Day —";
    renderSources();
  }
}

async function loadRecommendations({ quiet = false } = {}) {
  const horizon = toNumber(els.horizonSelect.value, 7);
  const slowNotice = window.setTimeout(() => {
    if (!state.recommendations.length) {
      els.recommendationList.innerHTML = `
        <div class="loading-state">
          <span class="loading-rune" aria-hidden="true"></span>
          <strong>${runtime.mode === "static" ? "Loading published forecasts" : "Calculating local forecasts"}</strong>
          <p>${runtime.mode === "static" ? "Reading the latest daily market snapshot and broad-league archive." : "Reading the current market and broad-league archive. The first calculation after startup can take a few seconds."}</p>
        </div>`;
    }
  }, 3000);
  try {
    const payload = await api(`/api/recommendations?horizon=${encodeURIComponent(horizon)}`);
    renderPayload({ horizon, ...payload });
  } catch (error) {
    if (!quiet) toast(`Could not run the model: ${error.message}`, "error");
    renderPayload({ horizon, recommendations: [] });
  } finally {
    window.clearTimeout(slowNotice);
  }
}

async function syncMarket() {
  if (runtime.mode === "static") {
    const workflowUrl = runtime.manifest?.workflow_url;
    if (!workflowUrl) {
      toast("This deployment does not include a GitHub update link.", "error");
      return;
    }
    window.open(workflowUrl, "_blank", "noopener,noreferrer");
    toast("Opened GitHub Actions. Use “Run workflow” to request an update.");
    return;
  }
  if (state.syncing) return;
  state.syncing = true;
  els.syncButton.disabled = true;
  els.syncButton.classList.add("syncing");
  els.syncButtonLabel.textContent = "Syncing supported markets…";
  setSourceState("warning", "Fetching fresh snapshots and extending the local exchange archive. Keep this page open.");

  try {
    const backfillHours = toNumber(els.backfillSelect.value, 0);
    const result = await api("/api/sync", {
      method: "POST",
      body: JSON.stringify({
        backfill_hours: backfillHours,
        horizon: toNumber(els.horizonSelect.value, 7),
      }),
    });
    toast(result?.message || "Market archive updated.");
    if (Array.isArray(result?.warnings) && result.warnings.length) {
      toast(result.warnings[0], "error");
    }
    await Promise.all([loadStatus({ quiet: true }), loadRecommendations({ quiet: true })]);
  } catch (error) {
    toast(`Sync stopped safely: ${error.message}`, "error");
    await loadStatus({ quiet: true });
  } finally {
    state.syncing = false;
    els.syncButton.disabled = false;
    els.syncButton.classList.remove("syncing");
    els.syncButtonLabel.textContent = "Sync market & run model";
  }
}

async function syncHistory() {
  if (runtime.mode === "static") {
    await syncMarket();
    return;
  }
  if (state.historySyncing) return;
  state.historySyncing = true;
  els.historyButton.disabled = true;
  const priorLabel = els.historyButton.textContent;
  els.historyButton.textContent = "Building completed-league archive…";
  els.historyMeta.textContent = "Fetching a resumable batch, preserving raw responses, and normalizing prices to Divine Orbs. Keep this page open.";

  try {
    const maxItems = Math.max(1, toNumber(els.historyBatchSelect.value, 80));
    const result = await api("/api/seasonal/backfill", {
      method: "POST",
      body: JSON.stringify({
        max_items: maxItems,
        horizon: toNumber(els.horizonSelect.value, 7),
      }),
    });
    toast(result?.message || "Past-league archive extended.");
    if (Array.isArray(result?.warnings) && result.warnings.length) {
      toast(result.warnings[0], "error");
    }
    await Promise.all([loadStatus({ quiet: true }), loadRecommendations({ quiet: true })]);
  } catch (error) {
    toast(`Historical backfill stopped safely: ${error.message}`, "error");
    await loadStatus({ quiet: true });
  } finally {
    state.historySyncing = false;
    els.historyButton.disabled = false;
    els.historyButton.textContent = priorLabel;
  }
}

function toast(message, kind = "success") {
  const element = document.createElement("div");
  element.className = `toast ${kind === "error" ? "error" : ""}`;
  element.textContent = message;
  els.toastRegion.appendChild(element);
  window.setTimeout(() => element.remove(), 6000);
}

function debounce(callback, delay = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), delay);
  };
}

const renderSearchResults = debounce(() => {
  renderRankingSummary();
  renderRecommendations();
}, 120);

els.syncButton.addEventListener("click", syncMarket);
els.historyButton.addEventListener("click", syncHistory);
els.horizonSelect.addEventListener("change", () => loadRecommendations());
els.categoryChecklist?.addEventListener("change", (event) => {
  const index = Number(event.target?.dataset?.categoryIndex);
  const category = Number.isInteger(index) ? state.availableCategories[index] : null;
  if (!category) return;
  setCategoryIncluded(category, Boolean(event.target.checked));
});
els.includeAllCategoriesButton?.addEventListener("click", includeAllCategories);
els.excludeAllCategoriesButton?.addEventListener("click", excludeAllCategories);
els.priceRangeFilter?.addEventListener("change", (event) => {
  state.priceRange = event.target.value;
  state.page = 1;
  renderRankingSummary();
  renderRecommendations();
});
els.searchInput.addEventListener("input", (event) => {
  state.query = event.target.value;
  state.page = 1;
  renderSearchResults();
});
els.resetHiddenItemsButton?.addEventListener("click", resetHiddenItems);
els.previousPageButton?.addEventListener("click", () => {
  state.page = Math.max(1, state.page - 1);
  renderRecommendations();
  els.recommendationList.querySelector("[data-open-detail]")?.focus();
});
els.nextPageButton?.addEventListener("click", () => {
  state.page += 1;
  renderRecommendations();
  els.recommendationList.querySelector("[data-open-detail]")?.focus();
});
els.pageSizeSelect?.addEventListener("change", (event) => {
  state.pageSize = Math.max(1, toNumber(event.target.value, 50));
  state.page = 1;
  renderRecommendations();
});
els.detailClose.addEventListener("click", () => els.detailDialog.close());
els.settingsButton.addEventListener("click", () => els.settingsDialog.showModal());
els.settingsClose.addEventListener("click", () => els.settingsDialog.close());
els.trustAction.addEventListener("click", () => els.settingsDialog.showModal());
els.refreshStatusButton.addEventListener("click", async () => {
  if (runtime.mode === "static") {
    window.location.reload();
    return;
  }
  await loadStatus();
  toast("Source status refreshed.");
});
window.addEventListener("storage", (event) => {
  if (![HIDDEN_ITEMS_STORAGE_KEY, CATEGORY_EXCLUSIONS_STORAGE_KEY].includes(event.key)) return;
  if (event.key === HIDDEN_ITEMS_STORAGE_KEY) {
    loadHiddenItems();
    renderHiddenItemsControl();
  } else {
    loadCategoryExclusions();
    renderCategoryOptions();
  }
  renderRankingSummary();
  renderRecommendations();
});

function configureRuntimeUi() {
  if (runtime.mode !== "static") return;
  els.syncButtonLabel.textContent = "Open update workflow";
  els.historyButton.hidden = true;
  els.backfillSelect.closest("label").hidden = true;
  els.historyBatchSelect.closest("label").hidden = true;
  els.refreshStatusButton.textContent = "Reload published data";
  els.archiveEyebrow.innerHTML = "<span>04</span> PUBLISHED ARCHIVE";
  els.dataTitle.textContent = "Daily history, published without the database.";
  els.archiveDescription.textContent = (
    "GitHub Actions retains the complete compressed SQLite archive separately "
    + "and publishes only the compact rankings and price-curve shards needed "
    + "by this page. Excluded markets remain archived for future research."
  );
  els.settingsTitle.textContent = "Published market archive";
  els.settingsIntro.textContent = (
    "This read-only snapshot is generated from the complete market archive by "
    + "a scheduled GitHub workflow. Open the update workflow to request a "
    + "manual refresh; no GitHub credential is stored in this page."
  );
  const loadingTitle = els.recommendationList.querySelector(
    ".loading-state strong",
  );
  const loadingDescription = els.recommendationList.querySelector(
    ".loading-state p",
  );
  if (loadingTitle) loadingTitle.textContent = "Opening the published ledger";
  if (loadingDescription) {
    loadingDescription.textContent = (
      "Loading the latest daily rankings and market archive."
    );
  }
}

async function initialize() {
  loadHiddenItems();
  loadCategoryExclusions();
  try {
    await discoverRuntime();
  } catch (error) {
    setSourceState("error", error.message);
    els.leagueName.textContent = "Published data unavailable";
    renderPayload({ horizon: 7, rankings: [] });
    return;
  }
  if (runtime.mode === "static") {
    configureRuntimeUi();
  }
  await Promise.all([
    loadStatus({ quiet: true }),
    loadRecommendations({ quiet: true }),
  ]);
}

initialize();
