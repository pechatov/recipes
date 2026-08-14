import assert from "node:assert/strict";
import { stat, unlink } from "node:fs/promises";


process.env.CART_ADAPTER_CONTROL_KEY = "test-control-key";
process.env.HERMES_ROOT = "/tmp/test-hermes";
process.env.HERMES_HOME = "/tmp/test-hermes-home";
process.env.CART_ADAPTER_STATE_FILE = `/tmp/recipes-cart-adapter-${process.pid}.json`;
process.env.CART_ADAPTER_QUARANTINE_FILE = `/tmp/recipes-cart-quarantine-${process.pid}.json`;

const {
  boundedOperationTimeout,
  deferScopeRecovery,
  errorBody,
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
