import assert from "node:assert/strict";


process.env.CART_ADAPTER_CONTROL_KEY = "test-control-key";
process.env.HERMES_ROOT = "/tmp/test-hermes";
process.env.HERMES_HOME = "/tmp/test-hermes-home";

const {
  boundedOperationTimeout,
  errorBody,
  finalBrowserError,
  OperationError,
  preserveMutationUncertainty,
  runExclusiveOperation,
  runWithOperationDeadline,
  sameLocation,
} = await import("./cart-adapter-server.mjs");

const bounded = await runWithOperationDeadline(
  async () => boundedOperationTimeout(300_000),
  1_000,
);
assert.ok(bounded > 0 && bounded <= 1_000);

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
