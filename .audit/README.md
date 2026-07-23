# .audit

Anti-pattern instrumentation ledger. Auto-managed — do not hand-edit.

## What's here

- **`counts.jsonl`** — one row per commit-to-`main`. Appended by
  `.github/workflows/antipattern-counts.yml` after every merge that
  touches `frontend/**`. Format:

  ```json
  {"ts": "2026-07-22T22:00:00Z", "sha": "abc1234", "tier1_you_might_not_need_effect": 3}
  ```

  Query:
  ```bash
  # Count trajectory over time
  jq -r '[.ts, .tier1_you_might_not_need_effect] | @tsv' .audit/counts.jsonl

  # Delta between commits
  jq -s 'to_entries | .[1:] | map({ts: .value.ts, sha: .value.sha, delta: (.value.tier1_you_might_not_need_effect - (.[.key - 1].value // {tier1_you_might_not_need_effect: 0}).tier1_you_might_not_need_effect)})' .audit/counts.jsonl
  ```

## The tiers this ledger measures

| Tier | What | Signal | Landing spot |
|---|---|---|---|
| 1 | ESLint `you-might-not-need-an-effect` | CI static analysis | This file (`counts.jsonl`) |
| ~~2~~ | ~~`@welldone-software/why-did-you-render`~~ | ~~Dev-mode console warnings~~ | **Dropped** — see below |
| 3 | React `<Profiler>` slow-commit sink | Runtime telemetry | `data/perf-profile.jsonl` (see `POST /api/perf/profile` in [src/web_service.py](../src/web_service.py)) |

Tier 1 is the only tier with a CI-time count (static analysis). Tier 3
needs a running environment to produce data.

## Why tier 2 was dropped

Attempted 2026-07-23 but couldn't cleanly compose with this project's
stack. Root cause: **wdyr's automatic-JSX runtime wraps every component
in a `WDYRFunctionalComponent` HOC to intercept renders**. React Router
6 does `child.type === Route` identity checks on `<Routes>` children —
the HOC wrapping breaks those checks, so no routes match and the app
renders blank.

Combination: **Vite 5 + `@vitejs/plugin-react` automatic JSX transform +
React 18 + `react-router-dom` 6 + wdyr 8** is a known compat wall. wdyr
can't patch what it needs to (JSX runtime) without breaking Router's
child-type discrimination.

Alternatives considered:
- **`exclude: [/Route/, /Routes/, ...]` in wdyr config** — unknown-depth
  debugging, more Router internals hit identity checks
- **Downgrade to `jsxRuntime: 'classic'` in dev** — requires
  `import React from 'react'` in every file (large refactor)
- **React DevTools 'Highlight updates when components render'** — a
  reasonable zero-code substitute; enable in the DevTools Settings gear
  if you want live re-render visualization

Tier 1 (static lint, catches the pattern class at PR time) and tier 3
(prod Profiler, catches the perf cost) cover the same anti-pattern
from both sides — the audit findings that inspired tier 2 (H4, H5, M4,
M6, M7) all get caught by tier 1's lint OR surface as slow commits in
tier 3's telemetry.

If wdyr ever fixes the Router compat wall, revisit — the `.wdyr.ts` /
`main.tsx` wiring pattern is documented in commit
`7105425` (the original instrumentation ship) as a starting point.

## Adding tier 3 to this ledger

If you want tier-3 counts joined into `counts.jsonl`, run an aggregator
against `data/perf-profile.jsonl`:

```bash
# Count slow commits by short-SHA
jq -r '.sha' data/perf-profile.jsonl \
  | sort | uniq -c \
  | awk '{print $2, $1}'

# Top 5 slowest commits observed
jq -r '.ev | [.actualDuration, .id, .phase] | @tsv' data/perf-profile.jsonl \
  | sort -k1 -rn | head -5
```

Wire that into the workflow if the number ever matters more than the
tier-1 signal.
