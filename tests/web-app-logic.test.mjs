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

test("static catalog and history shards are fetched once and POST is blocked", async () => {
  const result = await runInApp(`
    globalThis.__result = (async () => {
      let fetchCount = 0;
      globalThis.fetch = async (url) => {
        fetchCount += 1;
        const isHistory = String(url).includes("/history/");
        return {
          ok: true,
          status: 200,
          json: async () => isHistory
            ? { items: { "item:a": { current_league: { points: [{ league_day: 1, divine_value: 2 }] } } } }
            : { rankings: [{ key: "item:a" }] },
        };
      };
      runtime.mode = "static";
      runtime.manifest = {
        catalog: "data/catalog.abc.json",
        status: "data/status.abc.json",
        history_shards: { aa: "data/history/aa.abc.json" },
      };
      state.recommendations = [{ itemKey: "item:a", history_shard: "aa" }];
      const firstCatalog = await api("/api/recommendations?horizon=3");
      const secondCatalog = await api("/api/recommendations?horizon=14");
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
        firstHorizon: firstCatalog.horizon,
        secondHorizon: secondCatalog.horizon,
        firstHistory,
        secondHistory,
        postError,
      };
    })();
  `);

  assert.equal(result.fetchCount, 2);
  assert.equal(result.firstHorizon, 3);
  assert.equal(result.secondHorizon, 14);
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
