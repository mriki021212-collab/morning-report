from __future__ import annotations
import datetime as dt
import json
import os
import pathlib
import sys
import traceback

import jpholiday
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import analogs, collect, dashboard, jquants, news, notify, render, tdnet  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
JST = dt.timezone(dt.timedelta(hours=9))


def is_trading_day(d: dt.date) -> bool:
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    if (d.month, d.day) in [(1, 1), (1, 2), (1, 3), (12, 31)]:  # JPX年末年始休場
        return False
    return True


def build_facts(cfg: dict) -> dict:
    now = dt.datetime.now(JST)
    facts: dict = {
        "generated_at_jst": now.isoformat(),
        "trading_day": is_trading_day(now.date()),
        "sources": ["Yahoo Finance (yfinance) — 前営業日終値ベース",
                    "J-Quants API (JPX公式) — 信用/空売り",
                    "RSS: Reuters / 日経 / JPX / Yahooファイナンス"],
        "macro": {}, "holdings": {}, "sector": {}, "overseas_semis": {},
    }

    hist: dict[str, object] = {}
    for group in ("macro", "holdings", "sector", "overseas_semis"):
        for it in cfg[group]:
            df = collect.fetch_history(it["code"])
            hist[it["code"]] = df
            facts[group][it["code"]] = collect.snapshot(it["code"], it["name"], df)

    # 日経GU/GD: CME円建先物終値 と 日経現物終値の乖離（機械計算）
    n225, niy = facts["macro"].get("^N225", {}), facts["macro"].get("NIY=F", {})
    if n225.get("close") and niy.get("close"):
        # 先物のライブ値を分足で取得し、現物の引け時刻(15:00 JST)より新しいかで判定する。
        # 日付一致=無効 ではない。先物は24h動くので、同日付でも中身が現物より新しければ有効。
        live = collect.live_quote("NIY=F")
        cash_close_dt = dt.datetime.strptime(n225["as_of"], "%Y-%m-%d").replace(
            hour=15, minute=0, tzinfo=JST)
        fut_px, fut_ts, valid, warn = niy["close"], niy["as_of"] + " (日足)", None, None
        if live and live.get("price"):
            fut_ts_dt = dt.datetime.fromisoformat(live["ts_jst"])
            fut_px, fut_ts = live["price"], live["ts_jst"][:19]
            valid = fut_ts_dt > cash_close_dt
            if not valid:
                warn = (f"先物の最終値({fut_ts})が現物の引け({cash_close_dt:%Y-%m-%d %H:%M})"
                        "より古い。寄り付き示唆として使用不可。")
        else:
            valid = n225["as_of"] != niy["as_of"]
            warn = None if valid else "先物のライブ値を取得できず、日足の日付も現物と同一。使用不可。"
        facts["nikkei_gap"] = {
            "valid": bool(valid),
            "warning": warn,
            "futures_price_live": fut_px,
            "futures_as_of_jst": fut_ts,
            "cash_close_dt_jst": cash_close_dt.isoformat(),
            "n225_cash_close": n225["close"], "n225_cash_date": n225["as_of"],
            "cme_futures_close": niy["close"], "cme_date": niy["as_of"],
            "implied_gap_pts": round(fut_px - n225["close"], 1),
            "implied_gap_pct": round((fut_px / n225["close"] - 1) * 100, 2),
            "note": "先物-現物の単純乖離。配当落ち・限月要因は未調整。",
        }
    else:
        facts["nikkei_gap"] = {"status": "現時点では確認できない"}

    # 対SOX 60日相関
    sox = hist.get("^SOX")
    facts["correlation_vs_sox_60d"] = {"_note": "アジア市場(日本/韓国/台湾等)はSOXのD-1終値との相関(lag=1)。米国は同日(lag=0)。時差補正済み。"}
    for code in list(facts["holdings"]) + list(facts["sector"]) + list(facts["overseas_semis"]):
        df = hist.get(code)
        if sox is not None and df is not None and not df.empty and not sox.empty:
            # SOXが driver、当該銘柄が follower。アジア市場なら自動で lag=1
            facts["correlation_vs_sox_60d"][code] = collect.correlation(
                sox["Close"], df["Close"], lag=collect.market_lag("^SOX", code))

    # アナログ分析（保有3銘柄 + 日経）
    a = cfg["analog"]
    facts["analog"], facts["base_rate"] = {}, {}
    for code in [h["code"] for h in cfg["holdings"]] + ["^N225"]:
        df = hist.get(code)
        if df is not None and not df.empty:
            facts["analog"][code] = analogs.find_analogs(
                df, a["window"], a["horizon"], a["top_k"], a["min_history"])
            facts["base_rate"][code] = analogs.next_day_prob(df)

    # 履歴不足銘柄のピア代理アナログ（キオクシア等）
    facts["peer_proxy_analog"] = {}
    for code, spec in (cfg.get("peer_proxy") or {}).items():
        if facts["analog"].get(code, {}).get("status") is None:
            continue  # 本体で成立したなら代理不要
        entry = {"reason": spec["reason"], "peers": {},
                 "caution": "これはピア企業の過去分布であり、当該銘柄そのものの確率ではない。"}
        for pc in spec["peers"]:
            pdf = hist.get(pc)
            if pdf is None:
                pdf = collect.fetch_history(pc); hist[pc] = pdf
            if pdf is not None and not pdf.empty:
                entry["peers"][pc] = analogs.find_analogs(
                    pdf, a["window"], a["horizon"], a["top_k"], a["min_history"])
                # ピア(driver) vs 当該銘柄(follower)。米国ピアなら lag=1、韓国ピアなら lag=0
                entry.setdefault("correlation_285A_vs_peer_60d", {})[pc] = collect.correlation(
                    pdf["Close"], hist[code]["Close"], lag=collect.market_lag(pc, code)
                ) if hist.get(code) is not None else None
        facts["peer_proxy_analog"][code] = entry

    # データ品質サマリ（LLMに欠損を明示させるため）
    facts["data_quality"] = {
        c: {"history_days": len(hist[c]) if hist.get(c) is not None else 0,
            "analog_available": facts["analog"].get(c, {}).get("status") is None}
        for c in [h["code"] for h in cfg["holdings"]] + ["^N225"]
    }

    facts["margin_short"] = jquants.margin_and_short([h["code"] for h in cfg["holdings"]])
    facts["trades_spec"] = jquants.trades_spec()
    # B層+C層: 個別ニュース+マクロ地合い（取得失敗と該当なしを区別）
    facts["news"] = news.fetch(cfg["rss"])
    # A層: 保有銘柄の適時開示（一次情報・最優先）
    facts["tdnet"] = tdnet.fetch([h["code"] for h in cfg["holdings"]])
    facts["sentiment"] = {"status": "現時点では確認できない（X API/掲示板は未接続）"}
    facts["orderbook"] = {"status": "現時点では確認できない（リアルタイム板は取得範囲外）"}
    facts["events"] = {"status": "web_searchで確認すること"}
    return facts, hist


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    today = dt.datetime.now(JST).date()
    if not is_trading_day(today) and "--force" not in sys.argv:
        # 休場日も必ず1行投げる。
        # これをしないと「休場」「cron不発」「クラッシュ」が全部同じ"無音"になり、
        # 届かないことに気づけない。無音 = 異常、と意味を1つに固定する。
        print(f"{today} は東証休場。スキップ。")
        notify.post_holiday(today)
        return
    try:
        facts, hist = build_facts(cfg)
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        dashboard.write(facts, hist, out)
        (out / f"facts_{today:%Y%m%d}.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

        # 数値レポートは常に生成（API不要・幻覚ゼロ）
        report = render.render(facts)
        verdict = "数値のみ（LLM未使用）"

        use_llm = "--no-llm" not in sys.argv and os.getenv("ANTHROPIC_API_KEY")
        if use_llm:
            import analyst  # APIキーがある時だけ読み込む
            narrative = analyst.write_report(facts, cfg["model"], cfg["max_tokens"])
            verdict = analyst.audit(narrative, facts, "claude-sonnet-4-6")
            report = narrative + "\n\n---\n\n" + report
        else:
            print("LLM層はスキップ（ANTHROPIC_API_KEY未設定 または --no-llm）")

        (out / f"report_{today:%Y%m%d}.md").write_text(report, encoding="utf-8")
        notify.post(report, verdict, facts)
        print("posted. audit =", verdict)
    except Exception:
        notify.post_error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
