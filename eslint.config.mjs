// JS lint for the SPA, with no node_modules in the repo:
//   npx --yes eslint@10.13.0
// The config imports nothing (no @eslint/js, no `globals` package), so the
// rules from eslint:recommended that catch real bugs are listed by hand, and
// the browser globals the app uses are declared below.
const browser = Object.fromEntries([
  "window", "document", "location", "history", "navigator", "localStorage", "sessionStorage",
  "fetch", "URL", "URLSearchParams", "FormData", "Headers", "Event", "CustomEvent", "KeyboardEvent",
  "HTMLElement", "Element", "Node", "requestAnimationFrame", "cancelAnimationFrame",
  "setTimeout", "clearTimeout", "setInterval", "clearInterval", "matchMedia", "getComputedStyle",
  "IntersectionObserver", "MutationObserver", "ResizeObserver", "AbortController",
  "Intl", "console", "alert", "confirm", "crypto", "structuredClone", "queueMicrotask",
].map((name) => [name, "readonly"]));

export default [
  { ignores: ["scripts/vendor/", "spike/", "deploy/"] },
  {
    files: ["app/static/**/*.js", "app/organiser_ui/**/*.js"],
    languageOptions: { ecmaVersion: 2024, sourceType: "script", globals: browser },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      "no-undef": "error",
      "no-unused-vars": ["error", { args: "none", caughtErrors: "none" }],
      "no-redeclare": "error",
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-dupe-else-if": "error",
      "no-duplicate-case": "error",
      "no-unreachable": "error",
      "no-unsafe-finally": "error",
      "no-unsafe-negation": "error",
      "no-unsafe-optional-chaining": "error",
      "no-self-assign": "error",
      "no-self-compare": "error",
      "no-cond-assign": "error",
      "no-constant-condition": ["error", { checkLoops: false }],
      "no-constant-binary-expression": "error",
      "no-fallthrough": "error",
      "no-func-assign": "error",
      "no-const-assign": "error",
      "no-import-assign": "error",
      "no-loss-of-precision": "error",
      "no-sparse-arrays": "error",
      "no-unused-labels": "error",
      "no-useless-catch": "error",
      "no-empty-pattern": "error",
      "no-shadow-restricted-names": "error",
      "use-isnan": "error",
      "valid-typeof": "error",
      "getter-return": "error",
      "for-direction": "error",
      "eqeqeq": ["error", "smart"],
      "no-var": "error",
      "prefer-const": "error",
    },
  },
  { files: ["app/static/app.js", "app/organiser_ui/organiser.js"], languageOptions: { sourceType: "module" } },
];
