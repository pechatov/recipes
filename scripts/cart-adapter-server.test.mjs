import assert from "node:assert/strict";


process.env.CART_ADAPTER_CONTROL_KEY = "test-control-key";
process.env.HERMES_ROOT = "/tmp/test-hermes";
process.env.HERMES_HOME = "/tmp/test-hermes-home";

const {
  errorBody,
  preserveMutationUncertainty,
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
