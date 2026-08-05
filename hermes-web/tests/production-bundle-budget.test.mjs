import assert from "node:assert/strict";
import test from "node:test";
import {
  PRODUCTION_BUNDLE_BUDGET,
  assertProductionBundleBudget,
} from "../scripts/production-bundle-budget.mjs";

const withinBudget = {
  assetCount: 3,
  jsRawBytes: 350_000,
  jsGzipBytes: 100_000,
  cssRawBytes: 20_000,
  cssGzipBytes: 5_000,
};

test("accepts the current conservative production asset envelope", () => {
  assert.doesNotThrow(() => assertProductionBundleBudget(withinBudget));
  assert.deepEqual(PRODUCTION_BUNDLE_BUDGET, {
    maxAssetCount: 8,
    maxJsRawBytes: 450_000,
    maxJsGzipBytes: 130_000,
    maxCssRawBytes: 40_000,
    maxCssGzipBytes: 12_000,
  });
});

for (const [field, budgetField] of [
  ["assetCount", "maxAssetCount"],
  ["jsRawBytes", "maxJsRawBytes"],
  ["jsGzipBytes", "maxJsGzipBytes"],
  ["cssRawBytes", "maxCssRawBytes"],
  ["cssGzipBytes", "maxCssGzipBytes"],
]) {
  test(`rejects production output above ${budgetField}`, () => {
    assert.throws(
      () => assertProductionBundleBudget({
        ...withinBudget,
        [field]: PRODUCTION_BUNDLE_BUDGET[budgetField] + 1,
      }),
      new RegExp(`${field} exceeds production budget`),
    );
  });
}
