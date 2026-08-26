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
import analogs, collect, dashboard, earnings, jquants, news, notify, render, tdnet  # noqa: E402
import yahoo_jp  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
JST = dt.timezone(dt.timedelta(hours=9))


def is_trading_day(d: dt.date) -> bool:
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    if (d.month, d.day) in [(1, 1), (1, 2), (1, 3), (12, 31)]:  # JPX年末年始休場
        return False
    return True


def session_close_check(facts: dict) -> dict:
    """後場レポート用: 当日の日足が本当に確定しているかを国内銘柄だけで判定する。

    16:00 JST は大引け(15:30)の30分後で、yfinanceの日足がまだ当日分に更新されて
    いないことがある。その場合 snapshot の close は「前営業日の終値」であり、
    それを「本日の終値」として書くと、エラーを一切出さないまま日付が1日ずれた
    レポートが出る。このプロジェクトで最も避けたい失敗そのものなので、
    ここで機械的に検査して、確定していなければレポート側に明示させる。

    米国指数(^IXIC等)は16:00 JST時点で当日分が存在しなくて当たり前なので対象外。
    判定はティッカーの綴りではなく collect.is_asia() の市場区分で行う。
    """
    today = dt.datetime.now(JST).date().isoformat()
    confirmed, pending = [], []
    for group in ("holdings", "watch", "sector", "macro"):
        for code, s in facts.get(group, {}).items():
            if s.get("status") or not collect.is_asia(code):
                continue
            # yfinance日足以外のソース(TOPIX=Yahoo!ファイナンス日本)は更新タイミングが
            # 異なる。ここで一緒に判定すると、正常な株価データが揃っていても TOPIX の
            # 反映待ちだけで後場レポートの公開が止まる。このチェックはあくまで
            # 「yfinanceの日足が当日分に更新されたか」を見るものなので対象外にする。
            if s.get("source"):
                continue
            (confirmed if s.get("as_of") == today else pending).append(
                {"code": code, "name": s.get("name"), "as_of": s.get("as_of")})
    return {
        "expected_date": today,
        "confirmed": not pending and bool(confirmed),
        "confirmed_codes": [c["code"] for c in confirmed],
        "pending": pending,
        "warning": (
            None if not pending else
            f"国内{len(pending)}銘柄の日足がまだ当日({today})分に更新されていない。"
            "これらの終値は前営業日の値であり、本日の終値ではない。"
            "本日の値動きとして記述してはならない。"),
        "note": "米国市場は16:00 JST時点で当日分が存在しないため判定対象外。",
    }


def morning_vs_actual(facts: dict, out_dir: pathlib.Path) -> dict:
    """後場レポート用: 今朝の寄り付き前レポートの想定と、実際の着地を突き合わせる。

    朝の facts には先物から機械計算した寄り付き示唆(nikkei_gap.implied_gap_pct)がある。
    それと当日の実際の騰落率を並べるだけ。当たった/外れたの評価はしない（それは判断であり、
    LLM層か人間の仕事）。ここは差分という数値を出すところまで。
    """
    today = dt.datetime.now(JST).date()
    path = out_dir / f"facts_{today:%Y%m%d}.json"
    if not path.exists():
        return {"status": "現時点では確認できない（今朝のfactsファイルが存在しない）"}
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": f"取得失敗: {e}"}

    if m.get("session") == "afternoon":
        # 既に後場版で上書きされている＝今朝の寄り付き前スナップショットが残っていない
        return {"status": "現時点では確認できない（factsが後場版で上書き済み・朝の想定を復元できない）"}

    # 生成時刻が寄り付き(09:00)より後なら、それは「朝の想定」ではない。
    # 日中に --force で回すと同じファイル名で上書きされるため、session キーだけでは
    # 見分けられない。時刻を見ないと「今日の終値を今日の終値と比較して全部0.00%」という
    # 一見それらしい無意味な表を出してしまう（実際にそうなった）。
    gen = m.get("generated_at_jst", "")
    try:
        gen_dt = dt.datetime.fromisoformat(gen)
    except ValueError:
        return {"status": f"現時点では確認できない（factsの生成時刻を解釈できない: {gen!r}）"}
    if gen_dt.timetz() >= dt.time(9, 0, tzinfo=JST):
        return {"status": f"現時点では確認できない（比較対象のfactsが寄り付き後の生成 "
                          f"{gen_dt:%H:%M} JST。朝の想定として使えない）"}

    out = {"morning_generated_at": gen[:19]}
    g = m.get("nikkei_gap", {})
    n = facts.get("macro", {}).get("^N225", {})
    if g.get("valid") and n.get("chg_pct") is not None:
        out["nikkei"] = {
            "implied_gap_pct_at_morning": g.get("implied_gap_pct"),
            "actual_chg_pct": n.get("chg_pct"),
            "diff_pct_pt": round(n["chg_pct"] - g["implied_gap_pct"], 2),
            "note": "朝の先物示唆は寄り付きの乖離、実績は終値の前日比。始値と終値の違いを含むため一致は前提としない。",
        }
    else:
        out["nikkei"] = {"status": "現時点では確認できない（朝の先物示唆が使用不可だった）"}

    # 保有銘柄: 朝時点の確定終値(=前営業日)から当日終値までの変化
    out["holdings"] = {}
    for code, s in facts.get("holdings", {}).items():
        ms = m.get("holdings", {}).get(code, {})
        if s.get("status") or ms.get("status") or ms.get("close") in (None, 0):
            out["holdings"][code] = {"status": "現時点では確認できない"}
            continue
        out["holdings"][code] = {
            "name": s.get("name"),
            "morning_base_close": ms.get("close"),
            "morning_base_date": ms.get("as_of"),
            "actual_close": s.get("close"),
            "actual_date": s.get("as_of"),
            "chg_pct": round((s["close"] / ms["close"] - 1) * 100, 2),
        }
    return out


def _tracked_codes(cfg: dict) -> list[str]:
    """保有 + ウォッチ。ニュース/開示/信用/アナログの対象範囲。

    保有から外れた銘柄(6740)も分析対象には残す。保有パネルとポートフォリオ統計だけが
    holdings に限定され、それ以外の分析は連続性を優先する。
    """
    return ([h["code"] for h in cfg.get("holdings") or []]
            + [w["code"] for w in cfg.get("watch") or []])


def build_facts(cfg: dict, session: str = "morning") -> dict:
    now = dt.datetime.now(JST)
    facts: dict = {
        "generated_at_jst": now.isoformat(),
        "session": session,  # "morning"=寄り付き前 / "afternoon"=大引け後
        "trading_day": is_trading_day(now.date()),
        "sources": ["Yahoo Finance (yfinance) — 前営業日終値ベース",
                    "J-Quants API (JPX公式) — 信用/空売り",
                    "RSS: Reuters / 日経 / JPX / Yahooファイナンス"],
        "macro": {}, "holdings": {}, "watch": {}, "sector": {},
        "overseas_semis": {}, "sector_groups": {},
    }

    hist: dict[str, object] = {}
    for group in ("macro", "holdings", "watch", "sector", "overseas_semis"):
        for it in cfg.get(group) or []:
            df = collect.fetch_history(it["code"])
            hist[it["code"]] = df
            facts[group][it["code"]] = collect.snapshot(it["code"], it["name"], df)

    # 半導体以外の追加セクター(config.yaml の sector_groups:)。
    # sector: と同じ「構成銘柄の前日比の単純平均」で算出できるよう、構成銘柄の
    # スナップショットをセクター名ごとにまとめて持つ。members は sector: とは
    # 独立なので、半導体9銘柄の定義には一切影響しない。
    for gname, gspec in (cfg.get("sector_groups") or {}).items():
        facts["sector_groups"][gname] = {"members": {}}
        for it in gspec.get("members") or []:
            code = it["code"]
            if code not in hist:
                hist[code] = collect.fetch_history(code)
            facts["sector_groups"][gname]["members"][code] = collect.snapshot(
                code, it["name"], hist[code])

    # TOPIX。yfinanceに指数配信が無いため別ソース(Yahoo!ファイナンス日本)。
    # 取得できなければ status を持ったまま macro に入る = 画面は明示欠損になる。
    facts["macro"][yahoo_jp.TOPIX_CODE] = yahoo_jp.fetch_topix()

    # 投資信託(NISA)。株式とは別枠。テクニカルは算出しない。
    facts["funds"] = yahoo_jp.fetch_funds(cfg.get("funds"))

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
    for code in (list(facts["holdings"]) + list(facts["watch"])
                 + list(facts["sector"]) + list(facts["overseas_semis"])):
        df = hist.get(code)
        if sox is not None and df is not None and not df.empty and not sox.empty:
            # SOXが driver、当該銘柄が follower。アジア市場なら自動で lag=1
            facts["correlation_vs_sox_60d"][code] = collect.correlation(
                sox["Close"], df["Close"], lag=collect.market_lag("^SOX", code))

    # アナログ分析（保有 + ウォッチ + 日経）
    # ウォッチ(6740/285A)も対象に残す。保有から外れても分析の時系列を切らさないため。
    a = cfg["analog"]
    facts["analog"], facts["base_rate"] = {}, {}
    for code in _tracked_codes(cfg) + ["^N225"]:
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
        for c in _tracked_codes(cfg) + ["^N225"]
    }

    facts["margin_short"] = jquants.margin_and_short(_tracked_codes(cfg))
    facts["trades_spec"] = jquants.trades_spec()
    # B層+C層: 個別ニュース+マクロ地合い（取得失敗と該当なしを区別）
    facts["news"] = news.fetch(cfg["rss"])
    # A層: 保有銘柄の適時開示（一次情報・最優先）
    facts["tdnet"] = tdnet.fetch(_tracked_codes(cfg))
    facts["sentiment"] = {"status": "現時点では確認できない（X API/掲示板は未接続）"}
    facts["orderbook"] = {"status": "現時点では確認できない（リアルタイム板は取得範囲外）"}
    facts["events"] = {"status": "web_searchで確認すること"}

    if session == "afternoon":
        # 当日終値が本当に確定しているかの検査。ここがFalseなら後場レポートは
        # 「本日の値動き」を語ってはいけない（render/prompt の両方がこのキーを見る）。
        facts["session_close"] = session_close_check(facts)
        facts["morning_vs_actual"] = morning_vs_actual(facts, ROOT / "out")
        try:
            facts["earnings"] = earnings.build(cfg)
        except Exception as e:
            facts["earnings"] = {"status": f"取得失敗: {e}"}
    return facts, hist


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    # 大引け後の「本日の振り返り＋明日の予測」モード。無指定なら従来どおり寄り付き前レポート。
    session = "afternoon" if "--afternoon" in sys.argv else "morning"
    today = dt.datetime.now(JST).date()
    if not is_trading_day(today) and "--force" not in sys.argv:
        # 休場日も必ず1行投げる。
        # これをしないと「休場」「cron不発」「クラッシュ」が全部同じ"無音"になり、
        # 届かないことに気づけない。無音 = 異常、と意味を1つに固定する。
        print(f"{today} は東証休場。スキップ。")
        notify.post_holiday(today, session=session)
        return
    try:
        facts, hist = build_facts(cfg, session=session)
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        dashboard.write(facts, hist, out)
        # 決算カレンダーはレポート本体とは独立。落ちてもレポートは止めない。
        # ただし握りつぶさず、失敗はログに出して earnings.json の failed に残す。
        try:
            e = earnings.write(cfg, out)
            print(f"earnings: {len(e['events'])}件 / 取得不可 {len(e['failed'])}銘柄")
        except Exception:
            print("earnings の生成に失敗（レポートは続行）:\n" + traceback.format_exc())

        # 後場版は朝のファイルを上書きしない。
        # morning_vs_actual が今朝のスナップショットを読むため潰すと比較ができなくなる。
        # また score.py は report_*.md を採点対象にglobするので、後場版がそれに
        # マッチすると「本日の戦略」を持たないレポートを誤採点してしまう。
        stem = f"afternoon_{today:%Y%m%d}" if session == "afternoon" else f"report_{today:%Y%m%d}"
        fstem = f"facts_afternoon_{today:%Y%m%d}" if session == "afternoon" else f"facts_{today:%Y%m%d}"
        (out / f"{fstem}.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

        if session == "afternoon":
            sc = facts.get("session_close", {})
            if not sc.get("confirmed"):
                print("警告: 当日終値が未確定 — " + str(sc.get("warning")))

        # 数値レポートは常に生成（API不要・幻覚ゼロ）
        report = render.render_afternoon(facts) if session == "afternoon" else render.render(facts)
        verdict = "数値のみ（LLM未使用）"

        use_llm = "--no-llm" not in sys.argv and os.getenv("ANTHROPIC_API_KEY")
        if use_llm:
            import analyst  # APIキーがある時だけ読み込む
            narrative = analyst.write_report(facts, cfg["model"], cfg["max_tokens"], session=session)
            verdict = analyst.audit(narrative, facts, "claude-sonnet-4-6")
            report = narrative + "\n\n---\n\n" + report
        else:
            print("LLM層はスキップ（ANTHROPIC_API_KEY未設定 または --no-llm）")

        (out / f"{stem}.md").write_text(report, encoding="utf-8")
        # --no-post: 数値だけ更新したい時にDiscordへの投稿を止める。
        # 定時実行では絶対に付けないこと（無音 = 異常、の前提が崩れる）。
        if "--no-post" in sys.argv:
            print(f"generated ({session}) — Discord投稿はスキップ（--no-post）。out/{stem}.md")
            return
        notify.post(report, verdict, facts)
        print(f"posted ({session}). audit =", verdict)
    except Exception:
        if "--no-post" not in sys.argv:
            notify.post_error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
