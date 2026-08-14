import assert from "node:assert/strict";
import { stat, unlink } from "node:fs/promises";


process.env.CART_ADAPTER_CONTROL_KEY = "test-control-key";
process.env.HERMES_ROOT = "/tmp/test-hermes";
process.env.HERMES_HOME = "/tmp/test-hermes-home";
process.env.CART_ADAPTER_STATE_FILE = `/tmp/recipes-cart-adapter-${process.pid}.json`;

const {
  boundedOperationTimeout,
  errorBody,
  finalBrowserError,
  OperationError,
  operationFingerprint,
  preserveMutationUncertainty,
  readOperationRecord,
  runExclusiveOperation,
  runWithOperationDeadline,
  sameLocation,
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

await storeOperationRecord("scope:operation", {
  status: "started",
  fingerprint: "fingerprint",
});
assert.deepEqual(await readOperationRecord("scope:operation"), {
  status: "started",
  fingerprint: "fingerprint",
});
assert.equal((await stat(process.env.CART_ADAPTER_STATE_FILE)).mode & 0o777, 0o600);
await storeOperationRecord("scope:operation", {
  status: "completed",
  fingerprint: "fingerprint",
  result: { status: "applied" },
});
assert.equal(
  (await readOperationRecord("scope:operation")).result.status,
  "applied",
);
await unlink(process.env.CART_ADAPTER_STATE_FILE);

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
const firstMayFinish = new Promise((resolve) => {
  releaseFirst = resolve;
});
let starts = 0;
const first = runExclusiveOperation("same-cart", async () => {
  starts += 1;
  await firstMayFinish;
  return "first";
});
await assert.rejects(
  runExclusiveOperation("same-cart", async () => {
    starts += 1;
    return "queued";
  }),
  (error) => error.code === "scope_busy" && error.mutationPossible === true,
);
assert.equal(starts, 1, "a busy operation must fail instead of being queued");
releaseFirst();
assert.equal(await first, "first");
assert.equal(
  await runExclusiveOperation("same-cart", async () => {
    starts += 1;
    return "next";
  }),
  "next",
);
assert.equal(starts, 2, "the scope must be released after completion");
