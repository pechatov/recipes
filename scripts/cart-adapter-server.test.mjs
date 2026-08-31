import assert from "node:assert/strict";
import { stat, unlink } from "node:fs/promises";


process.env.CART_ADAPTER_CONTROL_KEY = "test-control-key";
process.env.HERMES_ROOT = "/tmp/test-hermes";
process.env.HERMES_HOME = "/tmp/test-hermes-home";
process.env.CART_ADAPTER_STATE_FILE = `/tmp/recipes-cart-adapter-${process.pid}.json`;
process.env.CART_ADAPTER_QUARANTINE_FILE = `/tmp/recipes-cart-quarantine-${process.pid}.json`;

const {
  boundedOperationTimeout,
  chooseLavkaAddress,
  deferScopeRecovery,
  errorBody,
  removeOperationRecord,
  storeSite,
  validatedLavkaContext,
  finalBrowserError,
  isScopeQuarantined,
  OperationError,
  operationFingerprint,
  pruneOperationRecords,
  preserveMutationUncertainty,
  quarantineScope,
  readOperationRecord,
  recoverQuarantinedScope,
  releaseScopeQuarantine,
  runExclusiveOperation,
  runWithOperationDeadline,
  sameLocation,
  selectStoreLink,
  classifyStorefrontUrl,
  searchQueries,
  scopeRecoveryAt,
  storeOperationRecord,
} = await import("./cart-adapter-server.mjs");

const bounded = await runWithOperationDeadline(
  async () => boundedOperationTimeout(300_000),
  1_000,
);
assert.ok(bounded > 0 && bounded <= 1_000);

const fingerprintItems = [
  { product_id: "product-b", sku_id: "sku-b", quantity: 2 },
  { product_id: "product-a", sku_id: "sku-a", quantity: 1 },
];
assert.equal(
  operationFingerprint("scope", "store", fingerprintItems),
  operationFingerprint("scope", "store", [...fingerprintItems].reverse()),
);
assert.notEqual(
  operationFingerprint("scope", "store", fingerprintItems),
  operationFingerprint("scope", "store", [
    { product_id: "product-b", sku_id: "sku-b", quantity: 3 },
    { product_id: "product-a", sku_id: "sku-a", quantity: 1 },
  ]),
);

const testStartedAt = new Date().toISOString();
await storeOperationRecord("scope:operation", {
  status: "started",
  fingerprint: "fingerprint",
  started_at: testStartedAt,
});
assert.deepEqual(await readOperationRecord("scope:operation"), {
  status: "started",
  fingerprint: "fingerprint",
  started_at: testStartedAt,
});
assert.equal((await stat(process.env.CART_ADAPTER_STATE_FILE)).mode & 0o777, 0o600);
await storeOperationRecord("scope:operation", {
  status: "completed",
  fingerprint: "fingerprint",
  completed_at: new Date().toISOString(),
  result: { status: "applied" },
});
assert.equal(
  (await readOperationRecord("scope:operation")).result.status,
  "applied",
);
await assert.rejects(
  storeOperationRecord(
    "scope:phantom",
    {
      status: "started",
      fingerprint: "fingerprint",
      started_at: new Date().toISOString(),
    },
    async () => {
      throw new Error("simulated durable write failure");
    },
  ),
  (error) => error.code === "operation_state_unavailable",
);
assert.equal(await readOperationRecord("scope:phantom"), null);
await unlink(process.env.CART_ADAPTER_STATE_FILE);

await quarantineScope("recipes-cart-user-42");
assert.equal(await isScopeQuarantined("recipes-cart-user-42"), true);
const recoveryStartedAt = Date.now();
const recoveryAt = await deferScopeRecovery(
  "recipes-cart-user-42",
  recoveryStartedAt,
);
assert.equal(recoveryAt, recoveryStartedAt + 120_000);
assert.equal(await scopeRecoveryAt("recipes-cart-user-42"), recoveryAt);
await assert.rejects(
  recoverQuarantinedScope("recipes-cart-user-42"),
  (error) => error.code === "profile_quarantined" && error.profileUncertain,
);
assert.equal((await stat(process.env.CART_ADAPTER_QUARANTINE_FILE)).mode & 0o777, 0o600);
await releaseScopeQuarantine("recipes-cart-user-42");
assert.equal(await isScopeQuarantined("recipes-cart-user-42"), false);
await unlink(process.env.CART_ADAPTER_QUARANTINE_FILE);

const retentionNow = Date.parse("2026-08-14T00:00:00Z");
const retentionRecords = {
  recent: {
    status: "completed",
    completed_at: "2026-08-13T00:00:00Z",
  },
  expiredCompleted: {
    status: "completed",
    completed_at: "2026-06-01T00:00:00Z",
  },
  expiredStarted: {
    status: "started",
    started_at: "2026-01-01T00:00:00Z",
  },
};
pruneOperationRecords(retentionRecords, retentionNow);
assert.deepEqual(Object.keys(retentionRecords), ["recent"]);

const internalError = preserveMutationUncertainty(
  new Error("failed after dispatch"),
  true,
);
assert.equal(internalError.mutationPossible, true);
assert.deepEqual(errorBody(internalError), {
  status: "failed",
  summary: "Адаптер корзины завершился с ошибкой.",
  error: "internal_error",
  mutation_possible: true,
});

const searchError = new Error("search failed");
assert.equal(
  finalBrowserError(searchError, new Error("close failed"), false),
  searchError,
);
assert.equal(searchError.mutationPossible, true);
assert.equal(searchError.profileUncertain, true);

const unavailableAfterCloseFailure = new OperationError(
  "store_unavailable",
  "store unavailable",
  { mutationPossible: true },
);
assert.deepEqual(errorBody(unavailableAfterCloseFailure), {
  status: "incomplete",
  summary: "store unavailable",
  mutation_possible: true,
});

assert.equal(
  sameLocation(
    { latitude: 55.7558, longitude: 37.6173 },
    { latitude: 55.755805, longitude: 37.617295 },
  ),
  true,
);
assert.equal(
  sameLocation(
    { latitude: 55.7558, longitude: 37.6173 },
    { latitude: 55.7568, longitude: 37.6173 },
  ),
  false,
);

assert.deepEqual(
  selectStoreLink("magnit", [
    {
      text: "Магнит Косметик",
      href: "https://eda.yandex.ru/retail/magnit_kosmetik?placeSlug=cosmetics-nearby",
    },
    {
      text: "Магнит · доставка продуктов",
      href: "https://eda.yandex.ru/retail/magnit_celevaya?placeSlug=grocery-nearby",
    },
  ]),
  {
    url: "https://eda.yandex.ru/retail/magnit_celevaya?placeSlug=grocery-nearby",
    placeSlug: "grocery-nearby",
    pathGroupSlug: "magnit_celevaya",
  },
);
assert.throws(
  () => selectStoreLink("magnit", [{
    text: "Магнит Косметик",
    href: "https://eda.yandex.ru/retail/magnit_kosmetik?placeSlug=cosmetics-nearby",
  }]),
  (error) => error.code === "store_unavailable",
);

assert.deepEqual(
  classifyStorefrontUrl(
    "perekrestok",
    "https://eda.yandex.ru/retail/perekrestok?placeSlug=perekryostok_nr5vg&relatedBrandSlug=perekrestok",
  ),
  {
    url: "https://eda.yandex.ru/retail/perekrestok?placeSlug=perekryostok_nr5vg",
    placeSlug: "perekryostok_nr5vg",
    pathGroupSlug: "perekrestok",
  },
);
assert.equal(
  classifyStorefrontUrl(
    "perekrestok",
    "https://eda.yandex.ru/retail?redirectFrom=not_found_place&relatedBrandSlug=perekrestok",
  ),
  null,
  "a brand bounce back to the landing page is an explicit miss",
);
for (const intermediate of [
  "https://eda.yandex.ru/retail/perekrestok",
  "https://eda.yandex.ru/retail/perekrestok?relatedBrandSlug=perekrestok",
  "https://eda.yandex.ru/retail",
  "https://eda.yandex.ru/retail/perekrestok_kafe?placeSlug=perekryostok_kafe_select_lxg5z",
  "https://eda.yandex.ru/retail/perekrestok/product/abc?placeSlug=perekryostok_nr5vg",
  "http://eda.yandex.ru/retail/perekrestok?placeSlug=perekryostok_nr5vg",
  "https://lavka.yandex.ru/",
  "",
]) {
  assert.equal(classifyStorefrontUrl("perekrestok", intermediate), undefined, intermediate);
}

assert.deepEqual(
  searchQueries("целая курица", "Курица"),
  ["целая курица", "Курица", "целая"],
);
assert.deepEqual(
  searchQueries("картофель для запекания", "Картофель"),
  ["картофель для запекания", "Картофель", "запекания"],
);
assert.deepEqual(searchQueries("Лимон свежий", "Лимон"), ["Лимон свежий", "Лимон"]);
assert.deepEqual(searchQueries("Молоко 3.2%", "молоко"), ["Молоко 3.2%", "молоко"]);

let releaseFirst;
let announceFirstStart;
const firstMayFinish = new Promise((resolve) => {
  releaseFirst = resolve;
});
const firstHasStarted = new Promise((resolve) => {
  announceFirstStart = resolve;
});
let starts = 0;
const exclusiveScope = "recipes-cart-user-77";
const first = runExclusiveOperation(exclusiveScope, async () => {
  starts += 1;
  announceFirstStart();
  await firstMayFinish;
  return "first";
});
await firstHasStarted;
assert.equal(await isScopeQuarantined(exclusiveScope), true);
await assert.rejects(
  runExclusiveOperation(exclusiveScope, async () => {
    starts += 1;
    return "queued";
  }),
  (error) => error.code === "scope_busy" && error.mutationPossible === true,
);
assert.equal(starts, 1, "a busy operation must fail instead of being queued");
releaseFirst();
assert.equal(await first, "first");
assert.equal(await isScopeQuarantined(exclusiveScope), false);
assert.equal(
  await runExclusiveOperation(exclusiveScope, async () => {
    starts += 1;
    return "next";
  }),
  "next",
);
assert.equal(starts, 2, "the scope must be released after completion");
await unlink(process.env.CART_ADAPTER_QUARANTINE_FILE);

// --- Яндекс Лавка helpers ---------------------------------------------------

assert.equal(storeSite("lavka"), "lavka");
assert.equal(storeSite("perekrestok"), "eda");

const lavkaFavorites = [
  {
    addressId: "01OLDADDRESS0000000000000A",
    address: { location: [37.5, 55.7], created: "2025-01-01T00:00:00Z" },
    tags: ["EDA", "HOME"],
  },
  {
    addressId: "01NEWADDRESS0000000000000B",
    address: { location: [37.6, 55.8], created: "2026-08-01T00:00:00Z" },
    tags: ["EDA"],
  },
  {
    addressId: "01WORKPLACE00000000000000C",
    address: { location: [37.7, 55.9], created: "2026-08-30T00:00:00Z" },
    tags: ["MARKET", "WORK"],
  },
];
assert.equal(
  chooseLavkaAddress(lavkaFavorites).address_id,
  "01NEWADDRESS0000000000000B",
  "the most recent EDA-tagged address wins over other tags",
);
assert.equal(
  chooseLavkaAddress([lavkaFavorites[2]]).address_id,
  "01WORKPLACE00000000000000C",
  "a single saved address is usable without the EDA tag",
);
assert.equal(
  chooseLavkaAddress([lavkaFavorites[2], {
    addressId: "01SECONDPLACE000000000000D",
    address: { location: [37.1, 55.1] },
    tags: [],
  }]),
  null,
  "several untagged addresses are ambiguous",
);
assert.equal(chooseLavkaAddress([]), null);
assert.equal(
  chooseLavkaAddress([{ addressId: "bad id", address: { location: [37.5, 55.7] }, tags: ["EDA"] }]),
  null,
  "a malformed address id is rejected",
);
assert.equal(
  chooseLavkaAddress([{ addressId: "01BROKEN00000000000000000E", address: { location: [null, 55.7] }, tags: ["EDA"] }]),
  null,
  "an address without coordinates is rejected",
);

const lavkaContext = validatedLavkaContext({
  site: "lavka",
  address_id: "01NEWADDRESS0000000000000B",
  latitude: 55.8,
  longitude: 37.6,
  store_url: "https://lavka.yandex.ru/",
});
assert.equal(lavkaContext.address_id, "01NEWADDRESS0000000000000B");
for (const broken of [
  {},
  { site: "eda", address_id: "01NEWADDRESS0000000000000B", latitude: 55.8, longitude: 37.6, store_url: "https://lavka.yandex.ru/" },
  { site: "lavka", address_id: "bad id", latitude: 55.8, longitude: 37.6, store_url: "https://lavka.yandex.ru/" },
  { site: "lavka", address_id: "01NEWADDRESS0000000000000B", latitude: 555, longitude: 37.6, store_url: "https://lavka.yandex.ru/" },
  { site: "lavka", address_id: "01NEWADDRESS0000000000000B", latitude: 55.8, longitude: 37.6, store_url: "https://eda.yandex.ru/retail" },
]) {
  assert.throws(
    () => validatedLavkaContext(broken),
    (error) => error.code === "invalid_selection",
    JSON.stringify(broken),
  );
}

await storeOperationRecord("scope:lavka-conflict", {
  status: "started",
  fingerprint: "lavka-fingerprint",
  started_at: new Date().toISOString(),
});
assert.ok(await readOperationRecord("scope:lavka-conflict"));
await removeOperationRecord("scope:lavka-conflict");
assert.equal(await readOperationRecord("scope:lavka-conflict"), null);
await removeOperationRecord("scope:lavka-conflict");
assert.equal(await readOperationRecord("scope:lavka-conflict"), null);
