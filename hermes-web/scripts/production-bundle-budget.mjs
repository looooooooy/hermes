export const PRODUCTION_BUNDLE_BUDGET = Object.freeze({
  maxAssetCount: 8,
  maxJsRawBytes: 450_000,
  maxJsGzipBytes: 130_000,
  maxCssRawBytes: 40_000,
  maxCssGzipBytes: 12_000,
});

export function assertProductionBundleBudget(metrics) {
  const limits = [
    ["assetCount", "maxAssetCount"],
    ["jsRawBytes", "maxJsRawBytes"],
    ["jsGzipBytes", "maxJsGzipBytes"],
    ["cssRawBytes", "maxCssRawBytes"],
    ["cssGzipBytes", "maxCssGzipBytes"],
  ];
  for (const [metric, budget] of limits) {
    const value = metrics[metric];
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new Error(`${metric} is not a valid production bundle measurement`);
    }
    if (value > PRODUCTION_BUNDLE_BUDGET[budget]) {
      throw new Error(`${metric} exceeds production budget: ${value} > ${PRODUCTION_BUNDLE_BUDGET[budget]}`);
    }
  }
}
