import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      "@typescript-eslint/consistent-type-imports": "error",
    },
  },
  {
    files: ["**/*.test.{ts,tsx}", "src/test/**/*.ts"],
    languageOptions: { globals: globals.vitest },
  },
  {
    files: ["**/*.{js,mjs}"],
    languageOptions: {
      globals: { ...globals.node, ...globals.browser, ...globals.worker },
    },
  },
);
