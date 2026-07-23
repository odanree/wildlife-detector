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

## The three tiers this ledger measures

| Tier | What | Signal | Landing spot |
|---|---|---|---|
| 1 | ESLint `you-might-not-need-an-effect` | CI static analysis | This file (`counts.jsonl`) |
| 2 | `@welldone-software/why-did-you-render` | Dev-mode console warnings | Local dev only — no CI signal |
| 3 | React `<Profiler>` slow-commit sink | Prod runtime telemetry | `data/perf-profile.jsonl` on the detector (see `POST /api/perf/profile` in [src/web_service.py](../src/web_service.py)) |

Tier 1 is the only one with a CI-time count because it's the only one
with a static-analysis signal. Tiers 2 and 3 need a running environment
(dev browser, prod server) to produce data.

## Adding tier 3 to this ledger

If you want tier-3 counts joined into `counts.jsonl`, run an aggregator
against the Pi's `data/perf-profile.jsonl`:

```bash
# On the Pi — count slow commits in the past 24h by short-SHA
jq -r '.sha' data/perf-profile.jsonl \
  | sort | uniq -c \
  | awk '{print $2, $1}'
```

Wire that into the workflow if the number ever matters more than the
tier-1 signal.
