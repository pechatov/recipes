import assert from "node:assert/strict";


process.env.CART_ADAPTER_CONTROL_KEY = "test-control-key";
process.env.HERMES_ROOT = "/tmp/test-hermes";
process.env.HERMES_HOME = "/tmp/test-hermes-home";

const {
  errorBody,
  preserveMutationUncertainty,
  runExclusiveOperation,
} = await import("./cart-adapter-server.mjs");

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
