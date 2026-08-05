"""
LLMを使わずに facts.json を読めるレポートに整形する。
ここに書かれる数値は100%計算結果であり、幻覚が原理的に混入しない。
解釈・戦略・★評価は含まない（それらは判断であり、LLMか人間の仕事）。
"""
from __future__ import annotations


def _n(v, unit="", d=2):
    if v is None:
        return "取得不可"
    if isinstance(v, (int, float)):
        return f"{v:,.{d}f}{unit}"
    return str(v)


def _arrow(v):
    if v is None:
        return "―"
    return f"{'▲' if v > 0 else '▼' if v < 0 else '－'}{abs(v):.2f}%"


def _sec(title):
    return f"\n\n## {title}\n"


def render(facts: dict) -> str:
    L = [f"# モーニングレポート（数値編）",
         f"生成: {facts['generated_at_jst'][:19]} JST / 東証: "
         f"{'営業日' if facts['trading_day'] else '休場'}",
         "\n> 本レポートは全数値がPythonの計算結果。解釈・売買判断は含まない。"]

    # ① 米国市場・マクロ
    L.append(_sec("① 前日の米国市場・マクロ"))
    L.append("| 指標 | 終値 | 前日比 | 日付 |")
    L.append("|---|---:|---:|---|")
    for code, s in facts["macro"].items():
        if s.get("status"):
            L.append(f"| {code} | 取得不可 | ― | ― |")
            continue
        L.append(f"| {s['name']} | {_n(s['close'])} | {_arrow(s['chg_pct'])} | {s['as_of']} |")

    # ② 日経ギャップ
    L.append(_sec("② 日経平均の寄り付き示唆（先物-現物の機械計算）"))
    g = facts.get("nikkei_gap", {})
    if g.get("status"):
        L.append("現時点では確認できない。")
    elif g.get("valid") is False:
        L.append(f"**使用不可** — {g['warning']}")
        L.append(f"（現物 {g['n225_cash_close']:,.0f}円 / 先物 {g['cme_futures_close']:,.0f}円 "
                 f"— 両方とも {g['n225_cash_date']} 付け）")
    else:
        L.append(f"- 現物終値 {_n(g['n225_cash_close'],'円',0)}（{g['n225_cash_date']}）")
        L.append(f"- CME円建先物 **{_n(g.get('futures_price_live') or g['cme_futures_close'],'円',0)}**"
                 f"（{g.get('futures_as_of_jst', g['cme_date'])} 時点のライブ値）")
        L.append(f"- **示唆される寄り付き乖離: {g['implied_gap_pts']:+,.0f}円 "
                 f"({g['implied_gap_pct']:+.2f}%)**")
        L.append(f"- 注: {g['note']}")

    # ③ 保有株
    L.append(_sec("③ 保有株テクニカル"))
    for code, s in facts["holdings"].items():
        if s.get("status"):
            L.append(f"\n### {code}\n{s['status']}")
            continue
        dq = facts.get("data_quality", {}).get(code, {})
        L.append(f"\n### {s['name']}（{code}） — {s['as_of']}")
        if s.get("stale_warning"):
            L.append(f"> ⚠️ **{s['stale_warning']}**")
        if s.get("split_warning"):
            L.append(f"> 🔀 **{s['split_warning']}**")
        L.append(f"終値 **{_n(s['close'],'円')}** / {_arrow(s['chg_pct'])} / "
                 f"出来高 {_n(s['volume'],'',0)}（20日平均比 {_arrow(s['volume_vs_20d_avg_pct'])}）")
        L.append("")
        L.append("| 項目 | 値 | 項目 | 値 |")
        L.append("|---|---:|---|---:|")
        L.append(f"| RSI(14) | {_n(s['rsi14'],'',1)} | MACD | {_n(s['macd'],'',3)} |")
        L.append(f"| MACDシグナル | {_n(s['macd_signal'],'',3)} | クロス | {s['macd_cross']} |")
        L.append(f"| 25日線 | {_n(s['ma25'],'円')} | 25日乖離 | {_arrow(s['dev_ma25_pct'])} |")
        L.append(f"| 75日線 | {_n(s['ma75'],'円')} | 75日乖離 | {_arrow(s['dev_ma75_pct'])} |")
        L.append(f"| 200日線 | {_n(s['ma200'],'円')} | 200日乖離 | {_arrow(s['dev_ma200_pct'])} |")
        L.append(f"| 60日高値 | {_n(s['high_60d'],'円')} | 60日安値 | {_n(s['low_60d'],'円')} |")
        L.append(f"| 52週高値 | {_n(s['high_52w'],'円')} | 52週安値 | {_n(s['low_52w'],'円')} |")
        L.append(f"| ATR(14) | {_n(s['atr14_pct'],'%')} | HV20(年率) | {_n(s['hv20_annual_pct'],'%')} |")
        L.append(f"| 出来高帯サポート | {_n(s['support_vp'],'円')} | 出来高帯レジスタンス | {_n(s['resistance_vp'],'円')} |")
        L.append(f"| ローソク足 | {s['candle']} | 対SOX相関(60日,lag1) | "
                 f"{_n(facts.get('correlation_vs_sox_60d',{}).get(code),'',2)} |")
        if dq:
            L.append(f"\n_履歴 {dq.get('history_days')}営業日 / "
                     f"アナログ分析: {'成立' if dq.get('analog_available') else '**不成立**'}_")

        # 信用・空売り
        ms = facts.get("margin_short", {}).get(code, {})
        if ms.get("margin_ratio") is not None:
            L.append(f"\n信用倍率 **{ms['margin_ratio']}倍**（買残 {ms['margin_long']:,.0f} / "
                     f"売残 {ms['margin_short']:,.0f}、{ms['margin_date']}時点）")
        else:
            L.append(f"\n信用倍率・空売り残: {ms.get('margin_status') or ms.get('status') or '取得不可'}")

    # ④ セクター
    L.append(_sec("④ 半導体セクター"))
    L.append("| 銘柄 | 終値 | 前日比 | RSI | 25日乖離 | 対SOX相関60日(lag1) |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for code, s in list(facts["sector"].items()) + list(facts["overseas_semis"].items()):
        if s.get("status"):
            continue
        L.append(f"| {s['name']} | {_n(s['close'])} | {_arrow(s['chg_pct'])} | "
                 f"{_n(s['rsi14'],'',1)} | {_arrow(s['dev_ma25_pct'])} | "
                 f"{_n(facts.get('correlation_vs_sox_60d',{}).get(code),'',2)} |")

    # ⑦ アナログ
    L.append(_sec("⑦ 過去チャート類似局面（実測頻度／予測ではない）"))
    for code, a in facts.get("analog", {}).items():
        L.append(f"\n### {code}")
        if a.get("status"):
            L.append(f"**{a['status']}** — {a.get('reason','')}")
            continue
        L.append(f"検索範囲 {a['sample_start']}〜{a['sample_end']}（{a['history_years']}年 / "
                 f"候補窓 {a['n_candidates_scanned']:,}）から上位{a['top_k_used']}局面を抽出")
        L.append(f"\n**その後{a['horizon']}営業日の実測:** "
                 f"上昇 {a['empirical_up_prob_pct']}% / 下落 {a['empirical_down_prob_pct']}% ・ "
                 f"中央値 {a['median_fwd_return_pct']:+}% ・ "
                 f"レンジ p10 {a['p10_fwd_return_pct']:+}% 〜 p90 {a['p90_fwd_return_pct']:+}%")
        L.append("\n| 類似日 | 一致率 | その後20日 |")
        L.append("|---|---:|---:|")
        for m in a["top_matches"][:8]:
            k = [x for x in m if x.startswith("fwd_")][0]
            L.append(f"| {m['date']} | {m['similarity_pct']}% | {m[k]:+}% |")
        L.append(f"\n_{a['note']}_")

    # ピア代理
    pp = facts.get("peer_proxy_analog", {})
    if pp:
        L.append(_sec("⑦-b 履歴不足銘柄のピア代理分布"))
        for code, e in pp.items():
            L.append(f"\n### {code}")
            L.append(f"理由: {e['reason']}")
            L.append(f"\n⚠️ {e['caution']}")
            L.append("\n| ピア | 上昇% | 下落% | 中央値 | 対{}相関60日 |".format(code))
            L.append("|---|---:|---:|---:|---:|")
            for pc, a in e["peers"].items():
                if a.get("status"):
                    L.append(f"| {pc} | 履歴不足 | ― | ― | ― |")
                    continue
                corr = e.get("correlation_285A_vs_peer_60d", {}).get(pc)
                L.append(f"| {pc} | {a['empirical_up_prob_pct']}% | "
                         f"{a['empirical_down_prob_pct']}% | {a['median_fwd_return_pct']:+}% | "
                         f"{_n(corr,'',2)} |")

    # ⑥-A 適時開示（TDnet・一次情報・最優先）
    L.append(_sec("⑥-A 保有銘柄の適時開示（TDnet）"))
    td = facts.get("tdnet", {})
    if td.get("status", "").startswith("取得失敗"):
        L.append(f"**取得失敗** — {td['status']}（開示ゼロではなく取得できていない）")
    elif not td.get("items"):
        L.append("直近48時間、保有銘柄の適時開示なし。")
    else:
        hs = td.get("high_signal", [])
        if hs:
            L.append("**要注意の開示:**")
            for i in hs:
                L.append(f"- 🚨 **[{i['company']}]** {i['title']}  "
                         f"（{i['time']} / {'・'.join(i['high_signal_words'])}）  "
                         f"[PDF]({i['url']})")
            L.append("")
        L.append("全開示:")
        for i in td["items"][:15]:
            L.append(f"- [{i['company']}] {i['title']} — {i['time']}  [PDF]({i['url']})")

    # ⑥-B 個別ニュース（保有株・セクター）
    L.append(_sec("⑥-B 保有株・セクター関連ニュース（RSS）"))
    nw = facts.get("news", {})
    if nw.get("status", "").startswith("全ソース取得失敗"):
        L.append(f"**取得失敗** — RSSに到達できていない。errors: {nw.get('errors')}")
    else:
        h = nw.get("holdings", [])
        se = nw.get("sector", [])
        if not h and not se:
            L.append("保有株・セクターに該当する報道は直近24時間なし。")
        for n in h[:10]:
            L.append(f"- **[{'・'.join(n['matched'])}]** [{n['title']}]({n['link']}) "
                     f"— {n['source']} / {n['published']}")
        for n in se[:8]:
            L.append(f"- [{'・'.join(n['matched'])}] [{n['title']}]({n['link']}) "
                     f"— {n['source']} / {n['published']}")
        if nw.get("errors"):
            L.append(f"\n_一部ソース取得失敗: {len(nw['errors'])}件_")

    # ⑥-C 全体まとめ（マクロ地合い）
    L.append(_sec("⑥-C 全体地合い（マクロRSS）"))
    mc = facts.get("news", {}).get("macro", [])
    if not mc:
        L.append("マクロ関連の該当記事は直近24時間なし。")
    for n in mc[:8]:
        L.append(f"- [{n['title']}]({n['link']}) — {n['source']} / {n['published']}")

    # 欠損一覧
    L.append(_sec("データ欠損一覧"))
    for k in ("orderbook", "sentiment", "events"):
        v = facts.get(k, {})
        L.append(f"- **{k}**: {v.get('status', '―')}")
    L.append("- **⑤市場心理 / ⑧本日の戦略 / ★重要度評価**: 本モード（無LLM）では出力しない。"
             "これらは数値ではなく判断であり、機械的に導出できないため。")

    L.append(_sec("出典"))
    for s in facts["sources"]:
        L.append(f"- {s}")
    L.append("\n_本レポートは投資助言ではない。売買判断は自己責任で。_")
    return "\n".join(L)


def _earnings_section(facts: dict) -> list[str]:
    """明日以降の決算予定。日付は earnings.json が持つものだけを出す（推定しない）。"""
    L = [_sec("④ 明日以降の決算予定")]
    e = facts.get("earnings", {})
    if e.get("status"):
        L.append(f"**{e['status']}**")
        return L
    ev, failed = e.get("events", []), e.get("failed", [])
    if not ev:
        L.append(f"{len(failed)}銘柄の決算予定を取得できなかった（{', '.join(failed)}）。"
                 if failed else "今後の決算予定なし。")
        return L
    L.append("| 日付 | 銘柄 | 確定/予定 | 保有 |")
    L.append("|---|---|---|---|")
    for x in ev[:10]:
        L.append(f"| {x['date']} | {x['name']}（{x['code']}） | "
                 f"{'確定' if x['confirmed'] else '**予定**'} | {'○' if x.get('held') else ''} |")
    if any(not x["confirmed"] for x in ev[:10]):
        L.append("\n_「予定」は企業が正式発表した日付ではない。変更されることがある。_")
    if failed:
        L.append(f"\n_取得できなかった銘柄: {', '.join(failed)}（推定日では埋めていない）_")
    return L


def render_afternoon(facts: dict) -> str:
    """大引け後の「本日の振り返り」。数値のみで、明日の予測（判断）は含まない。

    render() との最大の違いは、当日終値が確定しているかを最初に検査すること。
    未確定なら「本日の値動き」を一切書かずに打ち切る。前営業日の値を今日の値として
    出すくらいなら、何も出さないほうがよい。
    """
    sc = facts.get("session_close", {})
    L = ["# アフタヌーンレポート（数値編）",
         f"生成: {facts['generated_at_jst'][:19]} JST / 東証: "
         f"{'営業日' if facts['trading_day'] else '休場'}",
         "\n> 全数値はPythonの計算結果。解釈・明日の予測は含まない（それらは判断であり別レイヤの仕事）。"]

    # ⓪ 当日終値の確定チェック — ここが最重要。未確定なら以降を信用してはいけない
    L.append(_sec("⓪ 当日終値の確定状況"))
    if not sc:
        L.append("**検査未実施**（後場モードで生成されていない）。")
    elif sc.get("confirmed"):
        L.append(f"国内 {len(sc.get('confirmed_codes', []))}銘柄すべてが "
                 f"**{sc['expected_date']} の日足を確定済み**。以下の数値は本日の終値。")
    else:
        L.append(f"⚠️ **当日終値は未確定** — {sc.get('warning')}")
        L.append("\n以下の国内銘柄は前営業日の値のままである:")
        for p in sc.get("pending", []):
            L.append(f"- {p.get('name') or p['code']}（{p['code']}）— 最新確定 {p['as_of']}")
        L.append("\n**この状態では「本日の値動き」を記述できない。**"
                 "16:00 JST は大引け(15:30)の30分後で、yfinanceの日足が当日分に"
                 "更新されていないことがある。後続のcron（16:20 / 16:40）での再取得を待つこと。")
    if sc.get("note"):
        L.append(f"\n_{sc['note']}_")

    # ① 本日の着地
    L.append(_sec("① 本日の着地"))
    if sc and not sc.get("confirmed"):
        L.append("当日終値が未確定のため出力しない（前営業日の値を本日として出さない）。")
    else:
        L.append("| 銘柄 | 終値 | 前日比 | 出来高(20日平均比) | RSI | 25日乖離 |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for code, s in list(facts["holdings"].items()) + list(facts["sector"].items()):
            if s.get("status"):
                continue
            L.append(f"| {s['name']}（{code}） | {_n(s['close'],'円')} | {_arrow(s['chg_pct'])} | "
                     f"{_arrow(s['volume_vs_20d_avg_pct'])} | {_n(s['rsi14'],'',1)} | "
                     f"{_arrow(s['dev_ma25_pct'])} |")

    # ② 朝の想定との差分
    L.append(_sec("② 今朝の想定と実績の差"))
    mv = facts.get("morning_vs_actual", {})
    if mv.get("status"):
        L.append(f"**{mv['status']}**")
    else:
        L.append(f"今朝のレポート生成: {mv.get('morning_generated_at', '―')}")
        n = mv.get("nikkei", {})
        if n.get("status"):
            L.append(f"\n日経平均: {n['status']}")
        else:
            L.append(f"\n- 今朝の先物示唆（寄り付き乖離）: **{n['implied_gap_pct_at_morning']:+.2f}%**")
            L.append(f"- 実際の終値前日比: **{n['actual_chg_pct']:+.2f}%**")
            L.append(f"- 差: **{n['diff_pct_pt']:+.2f}%pt**")
            L.append(f"- _{n['note']}_")
        hs = mv.get("holdings", {})
        if hs:
            L.append("\n| 保有銘柄 | 今朝の基準終値 | 本日終値 | 変化 |")
            L.append("|---|---:|---:|---:|")
            for code, h in hs.items():
                if h.get("status"):
                    L.append(f"| {code} | ― | ― | {h['status']} |")
                    continue
                L.append(f"| {h['name']}（{code}） | {_n(h['morning_base_close'],'円')}"
                         f"（{h['morning_base_date']}） | {_n(h['actual_close'],'円')}"
                         f"（{h['actual_date']}） | {_arrow(h['chg_pct'])} |")

    # ③ 米国市場（これから動く材料）
    L.append(_sec("③ 米国市場・マクロ（前日終値／今夜これから動く）"))
    L.append("| 指標 | 終値 | 前日比 | 日付 |")
    L.append("|---|---:|---:|---|")
    for code, s in facts["macro"].items():
        if s.get("status"):
            L.append(f"| {code} | 取得不可 | ― | ― |")
            continue
        L.append(f"| {s['name']} | {_n(s['close'])} | {_arrow(s['chg_pct'])} | {s['as_of']} |")

    L.extend(_earnings_section(facts))

    # ⑤ 本日出た開示・ニュース
    L.append(_sec("⑤ 本日の適時開示（TDnet）"))
    td = facts.get("tdnet", {})
    if td.get("status", "").startswith("取得失敗"):
        L.append(f"**取得失敗** — {td['status']}（開示ゼロではなく取得できていない）")
    elif not td.get("items"):
        L.append("直近48時間、保有銘柄の適時開示なし。")
    else:
        for i in td.get("high_signal", []):
            L.append(f"- 🚨 **[{i['company']}]** {i['title']}  "
                     f"（{i['time']} / {'・'.join(i['high_signal_words'])}）  [PDF]({i['url']})")
        for i in td["items"][:12]:
            L.append(f"- [{i['company']}] {i['title']} — {i['time']}  [PDF]({i['url']})")

    L.append(_sec("⑥ 本日のニュース"))
    nw = facts.get("news", {})
    if nw.get("status", "").startswith("全ソース取得失敗"):
        L.append(f"**取得失敗** — RSSに到達できていない。errors: {nw.get('errors')}")
    else:
        rows = nw.get("holdings", [])[:8] + nw.get("sector", [])[:6] + nw.get("macro", [])[:6]
        if not rows:
            L.append("該当する報道は直近24時間なし。")
        for n in rows:
            tag = f"**[{'・'.join(n['matched'])}]** " if n.get("matched") else ""
            L.append(f"- {tag}[{n['title']}]({n['link']}) — {n['source']} / {n['published']}")

    L.append(_sec("データ欠損一覧"))
    for k in ("orderbook", "sentiment"):
        L.append(f"- **{k}**: {facts.get(k, {}).get('status', '―')}")
    L.append("- **明日の予測 / 市場心理 / ★重要度評価**: 本モード（無LLM）では出力しない。"
             "これらは数値ではなく判断であり、機械的に導出できないため。")

    L.append(_sec("出典"))
    for s in facts["sources"]:
        L.append(f"- {s}")
    L.append("\n_本レポートは投資助言ではない。売買判断は自己責任で。_")
    return "\n".join(L)
