// ESLint 9 flat config, scoped narrowly to ONE plugin:
// `eslint-plugin-react-you-might-not-need-an-effect`. Biome still owns
// everything else (formatting, imports, general lints, and the hook
// rules useExhaustiveDependencies + useHookAtTopLevel — see biome.json).
//
// Why the dual-linter setup: biome has no equivalent to these rules
// yet and no plugin API, so we run ESLint alongside biome purely for
// this class of check. Config surface stays tiny on purpose — if you
// find yourself adding rules here, ask whether biome can own them first.
//
// The plugin ships 10 rules; several map directly to findings from the
// 2026-07-22 React architectural audit:
//   - no-derived-state             ← H4 (workingPolygon mirror effect)
//   - no-adjust-state-when-a-prop-changes ← M6 (initialSeenId sync)
//   - no-initialize-state          ← M4 (useZoom mount re-init)
//   - no-event-handler             ← M7 (localStorage sync via effect)
// See PR #29 for the canonical fix shape (lift to parent + fully
// controlled child).

import tsParser from "@typescript-eslint/parser";
import youMightNotNeedAnEffect from "eslint-plugin-react-you-might-not-need-an-effect";

// Enable every rule the plugin ships, at 'error'. The plugin's own
// `configs.recommended` sets them to 'warn' (nice for adoption) but we
// want CI to fail — the counter workflow only fires on regressions,
// so making them errors is the mechanism that gives the counter teeth.
const rules = Object.fromEntries(
  Object.keys(youMightNotNeedAnEffect.rules).map((name) => [
    `react-you-might-not-need-an-effect/${name}`,
    "error",
  ]),
);

export default [
  {
    files: ["src/**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: "latest",
        sourceType: "module",
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      "react-you-might-not-need-an-effect": youMightNotNeedAnEffect,
    },
    rules,
  },
];
