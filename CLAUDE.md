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
- **`fx.py`** — USD/JPY only (`JPY=X`): 60 days of 1h bars, from which the daily bars are
  built (London-midnight windows, the same boundary Yahoo's own daily bars use), 20MA and its
  deviation, 20d high/low, the previous day's 1h high/low, and the US-JP 10y spread.
  **Yahoo's own JPY=X daily bars are not used**: measured 2026-09-22, every finalized bar's
  Close is the price just after the open (≈ Open), off from the real close by up to ~2.8 yen,
  while Yahoo's quote `previousClose` agreed with the 1h-derived close.
  Because `facts["macro"]["JPY=X"]` comes from `collect.snapshot()` — i.e. those same broken
  daily bars — `build_facts()` reconciles that row against this block via
  `fx.reconcile_macro_row()`: same close/prev_close/chg_pct/as_of, the replaced values kept
  under `superseded_yahoo_daily`, and a `close_basis` note. Without it the ① macro table and
  the dashboard strip disagreed with the ②-2 block about the same number (measured 2026-09-23:
  macro 157.863 as of 09-23 — actually the *still-forming* day's live price — vs fx 157.470
  as of 09-22, which is what Yahoo's own `fast_info.previousClose` returned). When `fx` fails
  the row is left exactly as it was — no substitute value is invented — and carries
  `close_basis_warning`, which `render.py`'s macro table, `dashboard.py` (`macro[].warn`, shown
  as a red ⚠ on the strip cell) and `notify.py`'s macro field all surface. **Only `JPY=X` is
  corrected**: measured 2026-09-23 over 30 finalized bars, its median |Close−Open| is 0.010%
  (max 0.035%), while CL=F 1.32%, GC=F 0.88% and NIY=F 0.25% (max 4.05%) have ordinary bars —
  do not extend this to other 24h instruments without re-measuring.
  Two rules that were deliberate, not incidental: (a) FX trades 24h, so a daily bar whose
  window has not closed yet is still forming — it is excluded from close/MA/high-low and surfaced
  separately as `forming_bar`, because a forming value printed as 前日終値 is exactly the
  silent-wrongness failure this repo exists to prevent; (b) there is no exchange close, so
  the "previous day" for the hourly bars is a JST calendar day, chosen to match the daily
  bar's date when possible and annotated (`fallback_note`) when it can't. The US 10y is
  reused from `facts["macro"]["^TNX"]` with **no unit conversion** and a range check
  (Yahoo has quoted ^TNX at 10x in the past); the JP 10y comes from MOF's official
  jgbcm.csv with strict parsing (wareki dates, header check, age and range checks) and
  falls back to 算出不可 rather than a plausible-looking number. The MOF parser was verified
  against the live CSV on 2026-09-22 (current-month file only, cp932, header on line 2, ends
  with a blank row and a ※ note row that the parser skips; publication lags ~1 business day).
- **`econ.py`** — US/JP economic calendar. **There is no automatic source**: every candidate
  (investing.com, Trading Economics, FMP, Nasdaq, Yahoo, ForexFactory mirror, FRED, BLS,
  federalreserve.gov, boj.or.jp) was probed and all were blocked by the dev container's
  egress proxy, so none could be verified — the full table is in the module docstring.
  Dates therefore come from `config.yaml econ_calendar.events`, written by hand from the
  official pages (never derived from "it's usually the first Friday"). `python src/econ.py
  --probe` re-runs the reachability check from a machine with network. The important safety
  property: when the last registered date is in the past, `status` becomes 要更新 rather
  than the calendar silently reading as 本日は予定なし.
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
- **`notify.py`** — Discord webhook posting: **exactly one message per run** — an embed built from
  `facts` (holdings / macro / gap / news headlines / factcheck / dashboard link, colour encoding the
  worst active condition) plus the full report attached as a `.md` file. It deliberately does NOT
  re-post the report body as chunked code blocks any more: that produced six messages a run, the ```
  fences killed every link, and the detail is already in the attachment and the dashboard. Keep the
  embed under Discord's limits (1024 chars per field value, 6000 per embed, 25 fields) — `_news_field`
  truncates and says so rather than silently dropping rows. Separate code paths exist for holiday/error
  notifications so that "market closed," "cron didn't fire," and "crashed" are never indistinguishable
  silence (see `main.py`'s holiday branch comment — this was a deliberate fix after a real missed-alert incident).

`config.yaml` defines the tracked instruments (`holdings`, `watch`, `sector`, `sector_groups`,
`funds`, `macro`, `overseas_semis`), the `fx` block (pair / bar counts / JGB CSV URL), the
hand-maintained `econ_calendar.events` list, the
`peer_proxy` substitution for stocks with too little history, `analog` search parameters, the RSS
source list for layer C, and the Claude model/max_tokens used.

`prompts/system.md` is the system prompt for the narrative-writing call — it encodes the "no invented
numbers," "separate fact from interpretation," the held-vs-watch separation, and the 285A
peer-proxy-disclosure rules that mirror the Python-side guarantees. If you change what `build_facts()` puts in `facts`, check whether this prompt
needs a corresponding update (e.g. a new "if this key is null, say so explicitly" rule).

## Operational notes relevant to code changes

- Scheduling runs both in GitHub Actions (`.github/workflows/morning.yml`, three staggered cron times
  as a hedge against GitHub's scheduler sometimes not firing at all) and via a Windows Scheduled Task
  on a desktop PC (`setup-desktop.ps1` registers it; `run-daily.ps1` is the generated runner — edit the
  here-string in `setup-desktop.ps1`, not just the committed copy, or the next setup run reverts you).
  Both call the same `src/main.py`; the morning run takes no flags, the afternoon run takes `--afternoon`
  — keep default (no-flag) behavior safe to run unattended.
- **The two schedulers do not know about each other, so `src/postguard.py` enforces "one notification per
  session per day" for both.** The shared state is `out/posted.json`, committed to the repo because the
  repository is the only channel the desktop task and the Actions runners have in common. It also holds
  the per-session time windows: a run that fires far outside its window posts a one-line "定時に発火せず"
  notice instead of the report, because a pre-open report delivered after the close is silently wrong.
  This was not theoretical — on 2026-08-28 the desktop posted the morning report at 08:30 JST and Actions
  posted it again at 16:00 JST, and the afternoon workflow fired at 04:10 JST on Saturday and sent holiday
  notices. `--force` bypasses both guards (manual runs must stay usable); `--no-post` skips them entirely.
  The afternoon run deliberately does NOT set the marker when the close is unconfirmed, so the 16:20/16:40
  retries can still take over — same rule as `afternoon.yml`.
- `.ps1` scripts in the repo root must stay ASCII-only — PowerShell 5.1 reads `.ps1` files as Shift-JIS,
  so any Japanese text in a script silently corrupts on save/read (noted explicitly in `setup-desktop.ps1`).
- `push.ps1` exists because two machines (laptop + desktop) share this repo via manual sync (not just
  scheduled pulls) — it refuses to stage `.venv/`, `out/`, `*.key`, `.env`.
- `src/quotes.py` writes `out/quotes.json` (intraday prices), driven by the committed `run-quotes.ps1`
  and a `MorningReport-Quotes` scheduled task (weekdays 09:00-15:30, every 15 min). That task went
  unregistered for three weeks (2026-08-26 → 08-31) because `setup-desktop.ps1` had not been re-run;
  it was re-registered on 2026-08-31. **Verified 2026-09-23**: `MorningReport-Quotes` is registered and
  firing every 15 min with rc=0 (`out/task.log` shows all 4 tickers fetched, `取得できない: (なし)`),
  alongside `MorningReport` 08:30, `MorningReport-PM` 16:00 and `MorningReport-News` 09:30/12:35/15:45.
  **A stale-looking `out/quotes.json` is usually correct, not a fault.** `run-quotes.ps1` commits only
  when a *price* changed — it diffs the file while ignoring `generated_at_jst`, and otherwise runs
  `git checkout -- out/quotes.json`. So on a holiday or a flat lunch break the file's mtime advances
  while its contents revert to the last committed version. Read `out/task.log` before concluding the
  task is broken. The dashboard guards against a genuinely stale file regardless: `liveFor()` discards
  any quote older than `CONFIG.quoteMaxAgeSec` (6h) so it can never overwrite the confirmed close
  (it did, for 21 days, before that guard). Keep the guard.
- **Don't re-run `setup-desktop.ps1` just to add or repair one task.** It `Unregister`s and recreates
  *every* task, rewrites `run-daily.ps1` from the here-string, and re-runs `pip install`. Without
  Administrator it also silently skips the wake-timer step. Register the single task you need instead.
- `out/` accumulates daily `facts_YYYYMMDD.json` and `report_YYYYMMDD.md` — these are real historical
  outputs (used by `score.py`), not disposable build artifacts; don't delete them as part of unrelated cleanup.
