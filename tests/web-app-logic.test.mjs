import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const appSource = fs
  .readFileSync(new URL("../web/app.js", import.meta.url), "utf8")
  .replace(/\ninitialize\(\);\s*$/, "\n");

function fakeElement(selector) {
  return {
    value: selector === "#pageSizeSelect"
      ? "50"
      : selector === "#horizonSelect"
        ? "7"
        : "",
    hidden: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    dataset: {},
    classList: {
      add() {},
      remove() {},
      toggle() {},
    },
    addEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    focus() {},
    close() {},
    showModal() {},
  };
}

function runInApp(statements) {
  const elements = new Map();
  const storage = new Map();
  const context = vm.createContext({
    URL,
    console,
    document: {
      baseURI: "https://owner.github.io/wraeclast-ledger/",
      querySelector(selector) {
        if (!elements.has(selector)) {
          elements.set(selector, fakeElement(selector));
        }
        return elements.get(selector);
      },
      querySelectorAll() {
        return [];
      },
      createElement() {
        return {
          textContent: "",
          remove() {},
          get innerHTML() {
            return this.textContent;
          },
        };
      },
    },
    fetch: async () => ({
      ok: true,
      json: async () => ({}),
    }),
    window: {
      addEventListener() {},
      clearTimeout() {},
      setTimeout() {
        return 1;
      },
      open() {},
      location: {
        reload() {},
      },
      localStorage: {
        getItem(key) {
          return storage.get(key) ?? null;
        },
        setItem(key, value) {
          storage.set(key, value);
        },
      },
    },
  });
  vm.runInContext(
    `${appSource}\n${statements}`,
    context,
    { filename: "web/app.js" },
  );
  return context.__result;
}

test("official trade links preserve league and exact Awakened gem variant", () => {
  const result = runInApp(`
    state.payload = { league: { id: "Allflame" } };
    globalThis.__result = {
      gem: officialTradeSearch({
        name: "Awakened Enlighten Support",
        category: "SkillGem",
        tradeIdentity: {
          variant: "5c",
          gemLevel: 5,
          gemQuality: null,
          corrupted: true,
        },
      }),
      forbidden: officialTradeSearch({
        name: "Forbidden Flame (Instruments of Virtue)",
        category: "ForbiddenJewel",
        tradeIdentity: {
          variant: "Forbidden Flame",
          passiveName: "Instruments of Virtue",
        },
      }),
    };
  `);

  const gemUrl = new URL(result.gem.url);
  const gemQuery = JSON.parse(gemUrl.searchParams.get("q"));
  assert.equal(gemUrl.pathname, "/trade/search/Allflame");
  assert.equal(
    gemQuery.query.type,
    "Awakened Enlighten Support",
  );
  assert.deepEqual(
    gemQuery.query.filters.misc_filters.filters.gem_level,
    { min: 5, max: 5 },
  );
  assert.deepEqual(
    gemQuery.query.filters.misc_filters.filters.quality,
    { min: 0, max: 0 },
  );
  assert.equal(
    gemQuery.query.filters.misc_filters.filters.corrupted.option,
    "true",
  );

  const forbiddenUrl = new URL(result.forbidden.url);
  const forbiddenQuery = JSON.parse(
    forbiddenUrl.searchParams.get("q"),
  );
  assert.equal(result.forbidden.broad, true);
  assert.equal(forbiddenQuery.query.name, "Forbidden Flame");
});

test("full item names wrap and include exact visible variant identity", () => {
  const result = runInApp(`
    const gem = normalizeRecommendation({
      key: "skillgem:awakened-enlighten-support-5c",
      name: "Awakened Enlighten Support",
      category: "SkillGem",
      trade_identity: {
        variant: "5c",
        gem_level: 5,
        corrupted: true,
      },
    }, 0);
    const jewel = normalizeRecommendation({
      key: "forbidden:flame-instruments-of-virtue",
      name: "Instruments of Virtue",
      category: "ForbiddenJewel",
      trade_identity: {
        variant: "Forbidden Flame",
        passive_name: "Instruments of Virtue",
      },
    }, 1);
    const flesh = normalizeRecommendation({
      key: "forbidden:flesh-instruments-of-virtue",
      name: "Instruments of Virtue",
      category: "ForbiddenJewel",
      trade_identity: {
        variant: "Forbidden Flesh",
        passive_name: "Instruments of Virtue",
      },
    }, 2);
    const base = normalizeRecommendation({
      key: "basetype:abyssal-axe-86-hunter-variant-hunter",
      name: "Abyssal Axe",
      category: "BaseType",
      trade_identity: {
        variant: "Hunter",
        item_level: 86,
      },
    }, 3);
    const clusterLevel = normalizeRecommendation({
      key: "clusterjewel:chaos-resistance-2-passives-84",
      name: "+12% to Chaos Resistance",
      category: "ClusterJewel",
      trade_identity: {
        variant: "2 passives",
        item_level: 84,
      },
    }, 4);
    const linkedUnique = normalizeRecommendation({
      key: "uniqueweapon:agnerod-east-6l-links-6",
      name: "Agnerod East",
      category: "UniqueWeapon",
      trade_identity: {
        base_type: "Imperial Staff",
        links: 6,
      },
    }, 5);
    const longName = "Adds 12 Passive Skills where the complete enchantment name must remain visible instead of being shortened by the ranking table";
    const cluster = normalizeRecommendation({
      key: "cluster:long-name",
      name: longName,
      category: "ClusterJewel",
    }, 2);
    globalThis.__result = {
      gem: gem.displayName,
      jewel: jewel.displayName,
      flesh: flesh.displayName,
      base: base.displayName,
      clusterLevel: clusterLevel.displayName,
      linkedUnique: linkedUnique.displayName,
      cluster: cluster.displayName,
    };
  `);

  assert.equal(
    result.gem,
    "Awakened Enlighten Support — Level 5 · corrupted",
  );
  assert.equal(
    result.jewel,
    "Forbidden Flame (Instruments of Virtue)",
  );
  assert.equal(
    result.flesh,
    "Forbidden Flesh (Instruments of Virtue)",
  );
  assert.equal(result.base, "Abyssal Axe — Item level 86 · Hunter");
  assert.equal(
    result.clusterLevel,
    "+12% to Chaos Resistance — Item level 84 · 2 passives",
  );
  assert.equal(result.linkedUnique, "Agnerod East — 6-link");
  assert.equal(
    result.cluster,
    "Adds 12 Passive Skills where the complete enchantment name must remain visible instead of being shortened by the ranking table",
  );

  const styles = fs.readFileSync(
    new URL("../web/styles.css", import.meta.url),
    "utf8",
  );
  const titleRule = styles.match(/\.item-cell strong\s*\{([^}]*)\}/)?.[1] || "";
  assert.match(titleRule, /white-space:\s*normal/);
  assert.match(titleRule, /overflow-wrap:\s*anywhere/);
  assert.doesNotMatch(titleRule, /text-overflow:\s*ellipsis/);
});

test("forbidden-jewel meta targets stay explicit beside raw history", () => {
  const result = runInApp(`
    const forecast = normalizeForecast({
      meta_multiplier: 1.25,
      forecast_7d: {
        days: 7,
        expected_gain_pct: 50,
        expected_price_divine: 1.35,
        historical_target_divine: 1.5,
        raw_historical_target_divine: 1.2,
        historical_target_gain_pct: 87.5,
        meta_multiplier: 1.25,
        historical_sample_leagues: 4,
      },
    }, 7);
    globalThis.__result = {
      multiplier: forecast.metaMultiplier,
      raw: forecast.rawHistoricalTargetDivine,
      adjusted: forecast.historicalTargetDivine,
      expectedPrice: forecast.expectedPriceDivine,
      cell: forecastCellMarkup(forecast, 7, 7),
      markup: forecastDetailMarkup(forecast, 7),
    };
  `);

  assert.equal(result.multiplier, 1.25);
  assert.equal(result.raw, 1.2);
  assert.equal(result.adjusted, 1.5);
  assert.equal(result.expectedPrice, 1.35);
  assert.match(result.cell, /expected 1\.35 div/);
  assert.doesNotMatch(result.cell, /expected 1\.50 div/);
  assert.match(result.markup, /META-ADJUSTED FUTURE TARGET/);
  assert.match(result.markup, /raw 1\.20 div × 1\.25/);
});

test("pagination and hidden-item filtering operate on the complete list", () => {
  const result = runInApp(`
    state.page = 2;
    state.pageSize = 2;
    state.hiddenItemKeys = new Set(["item:b"]);
    state.recommendations = [
      { itemKey: "item:a", searchText: "alpha currency", category: "Currency", priceDivine: 1 },
      { itemKey: "item:b", searchText: "beta currency", category: "Currency", priceDivine: 2 },
      { itemKey: "item:c", searchText: "gamma skillgem", category: "SkillGem", priceDivine: 3 },
      { itemKey: "item:d", searchText: "delta forbiddenjewel", category: "ForbiddenJewel", priceDivine: 4 },
      { itemKey: "item:e", searchText: "epsilon fragment", category: "Fragment", priceDivine: 5 },
    ];
    const filtered = filteredRecommendations();
    const page = paginatedRecommendations(filtered);
    globalThis.__result = {
      keys: filtered.map((item) => item.itemKey),
      pageKeys: page.items.map((item) => item.itemKey),
      total: page.total,
      totalPages: page.totalPages,
    };
  `);

  assert.deepEqual(
    [...result.keys],
    ["item:a", "item:c", "item:d", "item:e"],
  );
  assert.deepEqual([...result.pageKeys], ["item:d", "item:e"]);
  assert.equal(result.total, 4);
  assert.equal(result.totalPages, 2);
});

test("GitHub Pages paths and exact horizon ranks are repository-relative", () => {
  const result = runInApp(`
    runtime.mode = "static";
    const items = [
      normalizeRecommendation({
        key: "item:a",
        name: "Alpha",
        category: "Currency",
        rank: 1,
        static_ranks: { "3": 2, "7": 1, "14": 3 },
      }, 0),
      normalizeRecommendation({
        key: "item:b",
        name: "Beta",
        category: "SkillGem",
        rank: 2,
        static_ranks: { "3": 1, "7": 3, "14": 2 },
      }, 1),
      normalizeRecommendation({
        key: "item:c",
        name: "Gamma",
        category: "ForbiddenJewel",
        rank: 3,
        static_ranks: { "3": 3, "7": 2, "14": 1 },
      }, 2),
    ];
    rankStaticRecommendations(items, 14);
    globalThis.__result = {
      manifest: staticAssetUrl("data/manifest.json"),
      order: items.map((item) => item.itemKey),
      ranks: items.map((item) => item.rank),
    };
  `);

  assert.equal(
    result.manifest,
    "https://owner.github.io/wraeclast-ledger/data/manifest.json",
  );
  assert.deepEqual([...result.order], ["item:c", "item:b", "item:a"]);
  assert.deepEqual([...result.ranks], [1, 2, 3]);
});

test("static index, visible rank page, and history shard are each fetched once", async () => {
  const result = await runInApp(`
    globalThis.__result = (async () => {
      let fetchCount = 0;
      const fetchedUrls = [];
      globalThis.fetch = async (url) => {
        fetchCount += 1;
        fetchedUrls.push(String(url));
        const isHistory = String(url).includes("/history/");
        const isIndex = String(url).includes("ranking-index");
        const isRankingPage = String(url).includes("/rankings/");
        return {
          ok: true,
          status: 200,
          json: async () => {
            if (isHistory) {
              return { items: { "item:a": { current_league: { points: [{ league_day: 1, divine_value: 2 }] } } } };
            }
            if (isIndex) {
              return {
                fields: ["key", "name", "category", "search_text", "price_divine", "price_chaos", "rank_3d", "rank_7d", "rank_14d"],
                items: [["item:a", "Alpha", "Currency", "alpha currency", 2, 300, 1, 1, 1]],
              };
            }
            if (isRankingPage) {
              return {
                horizon: 7,
                page: 1,
                items: [{
                  key: "item:a",
                  name: "Alpha",
                  category: "Currency",
                  price_divine: 2,
                  history_shard: "aa",
                  static_ranks: { "3": 1, "7": 1, "14": 1 },
                  forecast_3d: { days: 3 },
                  forecast_7d: { days: 7, expected_gain_pct: 12 },
                  forecast_14d: { days: 14 },
                }],
              };
            }
            return { ranking_summary: { returned: 1 } };
          },
        };
      };
      runtime.mode = "static";
      runtime.manifest = {
        schema_version: 2,
        catalog: "data/catalog.abc.json",
        ranking_index: "data/ranking-index.abc.json",
        ranking_pages: {
          page_size: 100,
          horizons: {
            "3": ["data/rankings/3/0001.abc.json"],
            "7": ["data/rankings/7/0001.abc.json"],
            "14": ["data/rankings/14/0001.abc.json"],
          },
        },
        status: "data/status.abc.json",
        history_shards: { aa: "data/history/aa.abc.json" },
      };
      const firstCatalog = await api("/api/recommendations?horizon=3");
      const secondCatalog = await api("/api/recommendations?horizon=14");
      state.recommendations = firstCatalog.rankings.map(normalizeRecommendation);
      rankStaticRecommendations(state.recommendations, 7);
      await loadStaticPageDetails(state.recommendations, 7);
      await loadStaticPageDetails(state.recommendations, 7);
      const firstHistory = await api("/api/history?key=item%3Aa");
      const secondHistory = await api("/api/history?key=item%3Aa");
      let postError = "";
      try {
        await api("/api/sync", { method: "POST" });
      } catch (error) {
        postError = error.message;
      }
      return {
        fetchCount,
        fetchedUrls,
        firstHorizon: firstCatalog.horizon,
        secondHorizon: secondCatalog.horizon,
        expectedGain: state.recommendations[0].forecasts[7].expectedGainPct,
        firstHistory,
        secondHistory,
        postError,
      };
    })();
  `);

  assert.equal(result.fetchCount, 4);
  assert.equal(
    result.fetchedUrls.filter((url) => url.includes("ranking-index")).length,
    1,
  );
  assert.equal(
    result.fetchedUrls.filter((url) => url.includes("/rankings/")).length,
    1,
  );
  assert.equal(result.firstHorizon, 3);
  assert.equal(result.secondHorizon, 14);
  assert.equal(result.expectedGain, 12);
  assert.equal(
    result.firstHistory.seasonal_comparison.current_league.points[0].divine_value,
    2,
  );
  assert.deepEqual(
    JSON.parse(JSON.stringify(result.firstHistory)),
    JSON.parse(JSON.stringify(result.secondHistory)),
  );
  assert.match(result.postError, /read-only/i);
});

test("static filtered pages load only shards containing visible rows", async () => {
  const result = await runInApp(`
    globalThis.__result = (async () => {
      const fetched = [];
      globalThis.fetch = async (url) => {
        fetched.push(String(url));
        return {
          ok: true,
          status: 200,
          json: async () => ({
            items: [{
              key: "item:middle",
              name: "Middle",
              category: "SkillGem",
              price_divine: 5,
              static_ranks: { "3": 101, "7": 101, "14": 101 },
              forecast_3d: { days: 3 },
              forecast_7d: { days: 7 },
              forecast_14d: { days: 14 },
            }],
          }),
        };
      };
      runtime.mode = "static";
      runtime.manifest = {
        schema_version: 2,
        ranking_pages: {
          page_size: 100,
          horizons: {
            "7": [
              "data/rankings/7/0001.a.json",
              "data/rankings/7/0002.b.json",
              "data/rankings/7/0003.c.json",
            ],
          },
        },
      };
      const item = normalizeRecommendation({
        key: "item:middle",
        name: "Middle",
        category: "SkillGem",
        static_ranks: { "3": 101, "7": 101, "14": 101 },
      }, 0);
      item.rank = 101;
      await loadStaticPageDetails([item], 7);
      return { fetched, key: item.itemKey, rank: item.rank };
    })();
  `);

  assert.deepEqual(
    [...result.fetched],
    ["https://owner.github.io/wraeclast-ledger/data/rankings/7/0002.b.json"],
  );
  assert.equal(result.key, "item:middle");
  assert.equal(result.rank, 101);
});
