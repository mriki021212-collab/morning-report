# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python system that posts a pre-market stock report to Discord every JST weekday morning, covering
held stocks (7974 Nintendo / 5803 Fujikura) plus a non-held watch list (6740 Japan Display /
285A Kioxia) and the Japanese semiconductor sector.
Runs on a schedule via both GitHub Actions and a Windows Scheduled Task on a desktop PC.

## The core design: two/three-layer separation (read this before changing anything)

```
[layer 1] src/collect.py etc. — deterministic Python data collection & indicator math → facts_*.json
[layer 2] src/analyst.py      — Claude turns facts.json into prose, citing FACTS only, no invented numbers
[layer 3] src/analyst.py      — a second Claude call audits layer 2's numbers against FACTS
```

Feeding an LLM raw prices and asking it to reason about them produces hallucinated numbers. The fix is
a strict role split: **Python owns every number, Claude only writes prose about numbers Python already
computed.** `prompts/system.md` enforces this on the LLM side (rule 1: never invent a number not present
in FACTS); `src/render.py` enforces it on the no-LLM side by construction (it can only print what's in
facts.json). When adding a new fact, add it to `build_facts()` in `src/main.py` and to `src/render.py`'s
deterministic renderer — do not let `analyst.py`/the prompt introduce values that don't come from FACTS.

The system runs fully without any LLM (`--no-llm` or no `ANTHROPIC_API_KEY`): `src/render.py` alone
produces a complete deterministic report. The LLM layer only adds ⑤ market psychology, ⑧ strategy, and
★ importance ratings — judgment calls that can't be computed. Keep this fallback path working; it's the
primary safety net against hallucination and against API cost/outage.

## Commands

```powershell
# Local dev (Windows / PowerShell — this is a Windows-only project)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run without any API keys (deterministic-only, recommended starting point)
python src/main.py --force --no-llm

# Full run with LLM narrative + audit (needs ANTHROPIC_API_KEY, DISCORD_WEBHOOK_URL)
python src/main.py --force        # --force runs even outside JST trading hours/on holidays

# Score past reports' stance calls (⑧本日の戦略) against actual subsequent returns
python src/score.py 30            # scores the last 30 out/report_*.md files

# Connectivity checks (must be run from a real machine — sandboxes have RSS/network blocked)
.\.venv\Scripts\python.exe -c "import src.tdnet as t; import json; print(json.dumps(t.fetch(['7974.T','5803.T','6740.T','285A.T']), ensure_ascii=False, indent=1))"
```

There is no test suite, linter, or type checker configured in this repo — validate changes by running
`src/main.py --force --no-llm` and inspecting the generated `out/facts_YYYYMMDD.json` / `out/report_YYYYMMDD.md`.

## Architecture

`src/main.py` is the orchestrator: `build_facts(cfg)` calls into every collector module, assembles the
single `facts` dict, and `main()` decides trading-day/holiday, LLM vs. no-LLM, writes outputs, and posts
to Discord. Everything downstream depends on `facts` having the shape `build_facts` produces — when a
collector returns nothing/fails, it must fill in a `"status": "現時点では確認できない"` (or `取得失敗`)
sentinel string rather than omitting the key or faking a value, because `render.py` and the LLM prompt
both key off exactly that convention to distinguish "no data" from "confirmed nothing."

Collector modules (`src/collect.py`, `src/jquants.py`, `src/news.py`, `src/tdnet.py`, `src/analogs.py`):
- **`collect.py`** — yfinance price history, all technical indicators (RSI/MACD/MAs/ATR/HV/support-resistance),
  and cross-market correlation. Read `market_lag()`/`is_asia()` before touching any cross-market
  correlation code: US-market closes finalize ~14h after Asian closes, so same-day correlation between
  e.g. SOX and a Tokyo stock is meaningless noise (this was a real bug, documented in README). The rule
  is decided by timezone (`is_asia()`), never by ticker suffix pattern-matching.
- **`jquants.py`** — J-Quants API (margin ratio, short positions, investor-type flows). Requires
  `JQ_REFRESH_TOKEN`; returns the "unavailable" sentinel dict when absent rather than raising.
- **`yahoo_jp.py`** — the two things yfinance cannot supply: the TOPIX index (yfinance 404s on every
  TOPIX symbol; `1306.T` is an ETF, not the index) and Japanese mutual-fund NAVs. This is HTML
  scraping of Yahoo!ファイナンス(日本) and will break if their page structure changes — when it does,
  it must return a `status` string, never a previous or plausible-looking value. The module docstring
  records every source that was tried and why it was rejected; read it before swapping the source.
- **`news.py`** — two independently-queried layers: per-holding/per-sector Google News RSS search (layer
  B) and macro newspaper RSS filtered by keyword (layer C). Always distinguishes fetch failure from
  "no matching articles" in its `status` field — don't collapse that distinction when editing.
- **`tdnet.py`** — TDnet regulatory disclosures via the yanoshin API, fetched directly by stock code
  (not keyword-matched) so litigation/earnings-revision disclosures can't be missed by a keyword miss.
  Same failure-vs-empty distinction applies.
- **`analogs.py`** — "similar past chart pattern" search: normalizes the last N days' log-return series,
  finds the closest-distance historical windows since 2005, and reports the *empirical* forward-return
  distribution of those matches. This is how the system produces an "up probability" without the LLM
  guessing one — it's a measured frequency, not a model output. Has statistical gates (`MIN_YEARS`,
  `MIN_CANDIDATES`, `MAX_TOPK_RATIO`) that return the "insufficient history" status instead of a
  low-confidence result; 285A (Kioxia, listed Dec 2024) fails these gates and falls back to
  `peer_proxy_analog` (proxy stocks defined in `config.yaml`) — preserve this fallback when editing.

Output modules:
- **`render.py`** — pure functions from `facts` dict to Markdown. No interpretation/judgment, no API
  calls; every value printed must trace back to a `facts` key. This is the always-available fallback report.
- **`analyst.py`** — two Claude API calls: `write_report()` (writes the narrative using `prompts/system.md`
  as system prompt, `facts` JSON as the only permitted source of numbers, plus a bounded `web_search` tool
  for events/news only) and `audit()` (a second, different-model call that cross-checks every number in
  the narrative against `facts` and returns `OK` or a list of discrepancies — this result is surfaced in
  the Discord embed color/footer).
- **`dashboard.py`** — writes `out/dashboard.json`, consumed by `terminal_dashboard.html` (the page
  published to GitHub Pages as both `/` and `/terminal_dashboard.html`). There is NO demo/simulated
  fallback: if the JSON is missing the page says so and renders nothing. The older `dashboard.html`
  still exists in the repo but is deliberately excluded from the published site because it does have
  a seed-data demo path. Do not re-add it to `pages.yml`.
  `sectors` is a name-to-number map and must stay that shape; per-sector member lists and the
  averaging method live in the separate `sector_defs` key. `holds` (held) and `watch` (not held)
  share one row shape but must never be merged — only `holds` feeds the portfolio statistics.
- **`notify.py`** — Discord webhook posting: builds embed fields/color from `facts`, chunks the long
  report body under Discord's message-length limit, and has separate code paths for holiday/error
  notifications so that "market closed," "cron didn't fire," and "crashed" are never indistinguishable
  silence (see `main.py`'s holiday branch comment — this was a deliberate fix after a real missed-alert incident).

`config.yaml` defines the tracked instruments (`holdings`, `watch`, `sector`, `sector_groups`,
`funds`, `macro`, `overseas_semis`), the
`peer_proxy` substitution for stocks with too little history, `analog` search parameters, the RSS
source list for layer C, and the Claude model/max_tokens used.

`prompts/system.md` is the system prompt for the narrative-writing call — it encodes the "no invented
numbers," "separate fact from interpretation," the held-vs-watch separation, and the 285A
peer-proxy-disclosure rules that mirror the Python-side guarantees. If you change what `build_facts()` puts in `facts`, check whether this prompt
needs a corresponding update (e.g. a new "if this key is null, say so explicitly" rule).

## Operational notes relevant to code changes

- Scheduling runs both in GitHub Actions (`.github/workflows/morning.yml`, three staggered cron times
  as a hedge against GitHub's scheduler sometimes not firing at all) and via a Windows Scheduled Task
  on a desktop PC (`setup-desktop.ps1` registers it; `run-daily.ps1` is the generated runner). Both call
  the same `src/main.py` with no special flags — keep default (no-flag) behavior safe to run unattended.
- `.ps1` scripts in the repo root must stay ASCII-only — PowerShell 5.1 reads `.ps1` files as Shift-JIS,
  so any Japanese text in a script silently corrupts on save/read (noted explicitly in `setup-desktop.ps1`).
- `push.ps1` exists because two machines (laptop + desktop) share this repo via manual sync (not just
  scheduled pulls) — it refuses to stage `.venv/`, `out/`, `*.key`, `.env`.
- `src/quotes.py` writes `out/quotes.json` (intraday prices) but is NOT wired to any scheduler.
  The published file is therefore frozen at whenever it was last run by hand. The dashboard
  guards against this: `liveFor()` discards any quote older than `CONFIG.quoteMaxAgeSec` so a
  stale file can never overwrite the confirmed close (it did, for 21 days, before that guard).
  If you wire it up, keep the guard.
- `out/` accumulates daily `facts_YYYYMMDD.json` and `report_YYYYMMDD.md` — these are real historical
  outputs (used by `score.py`), not disposable build artifacts; don't delete them as part of unrelated cleanup.
