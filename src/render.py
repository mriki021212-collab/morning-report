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
    else:
        L.append(f"- 現物終値 {_n(g['n225_cash_close'],'円',0)}（{g['n225_cash_date']}）")
        L.append(f"- CME円建先物 {_n(g['cme_futures_close'],'円',0)}（{g['cme_date']}）")
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

    # ⑥ ニュース
    L.append(_sec("⑥ 直近24時間のニュース（RSS / 見出しのみ）"))
    news = facts.get("news", [])
    if not news:
        L.append("該当なし。")
    for n in news[:15]:
        if n.get("error"):
            continue
        L.append(f"- [{n['title']}]({n['link']}) — {n['source']} / {n['published'][:16]}")

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
