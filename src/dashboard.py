"""
ダッシュボード用 JSON を書き出す。dashboard.html がこれを読んで実データを表示する。
無ければHTML側はデモにフォールバックするので、これは「あれば実データ」の位置づけ。
"""
from __future__ import annotations
import json
import pathlib


def _spark_from(df, n=40):
    if df is None or df.empty:
        return []
    return [round(float(x), 2) for x in df["Close"].tail(n).tolist()]


def _chart_hist(df, n=260):
    """中央チャート用: 実際の取引日付+終値を最大n営業日ぶん返す。期間ボタンはこの配列を
    クライアント側でスライスするだけで、存在しない期間のデータを作り出さない。"""
    if df is None or df.empty:
        return {"d": [], "c": []}
    tail = df.tail(n)
    return {
        "d": [ts.strftime("%Y-%m-%d") for ts in tail.index],
        "c": [round(float(x), 2) for x in tail["Close"].tolist()],
    }


def build(facts: dict, hist: dict) -> dict:
    holds = []
    for code, s in facts.get("holdings", {}).items():
        if s.get("status"):
            continue
        holds.append({
            "code": code, "nm": s["name"], "px": s["close"], "base": s["close"],
            "chg": s["chg_pct"], "baseChg0": s["chg_pct"],
            "rsi": s["rsi14"], "ma25": s["dev_ma25_pct"],
            "vol": f"{s['volume']/1e6:.1f}M" if s.get("volume") else "—",
            "candle": s.get("candle", "—"),
            "flag": None,  # 高シグナル開示があれば下で差し込む
            "s": _spark_from(hist.get(code)),
            "hist": _chart_hist(hist.get(code)),
        })

    # TDnet高シグナルを flag に反映
    for i in facts.get("tdnet", {}).get("high_signal", []):
        for h in holds:
            if h["code"].startswith(i.get("code", "")):
                h["flag"] = "・".join(i["high_signal_words"][:2]) + " 開示"
                h["hot"] = True

    # config.yaml の macro: に登録されている全指標を出力する。
    # ここで一部だけに絞ると「取得済みなのに表示されない」欠損を自作することになる。
    macro_keys = [
        ("^N225", "日経225"), ("^SOX", "SOX 半導体指数"), ("^IXIC", "NASDAQ"),
        ("^GSPC", "S&P 500"), ("^DJI", "NYダウ"), ("^VIX", "VIX 恐怖指数"),
        ("JPY=X", "USD/JPY"), ("^TNX", "米10年債利回り"), ("CL=F", "WTI原油"),
        ("GC=F", "金(GOLD)"), ("NIY=F", "日経平均先物(CME円建)"),
    ]
    macro = []
    for code, label in macro_keys:
        m = facts.get("macro", {}).get(code, {})
        if m.get("close") is not None:
            macro.append({"k": label, "v": f"{m['close']:,.2f}", "c": m.get("chg_pct", 0)})

    tape = []
    for code, s in facts.get("sector", {}).items():
        if not s.get("status"):
            tape.append([f"{code.replace('.T','')} {s['name']}", s["close"], s["chg_pct"]])
    for code, s in facts.get("overseas_semis", {}).items():
        if not s.get("status"):
            tape.append([code, s["close"], s["chg_pct"]])

    # セクター騰落率（構成銘柄の前日比・単純平均）。
    # 定義: sum(chg_pct) / 構成銘柄数（取得できた銘柄のみで平均。加重ではない）。
    # 現状データソースがあるのは日本半導体セクター(config.yamlのsector:)のみ。
    # 他セクター(AI/銀行/商社/自動車/防衛/エネルギー/不動産)は収集元が存在しないため
    # ここに追加しない = terminal_dashboard.html側で「データなし」表示のまま。
    sectors = {}
    semi_chgs = [s["chg_pct"] for s in facts.get("sector", {}).values() if not s.get("status")]
    if semi_chgs:
        sectors["半導体"] = round(sum(semi_chgs) / len(semi_chgs), 4)

    feed = []
    for i in facts.get("tdnet", {}).get("items", [])[:6]:
        feed.append({"tm": i["time"][-5:], "tag": "td",
                     "hot": bool(i.get("high_signal_words")),
                     "matched": i.get("high_signal_words", []),
                     "url": i.get("url") or None, "source": "TDnet",
                     "html": f"<b>{i['company']}</b> {i['title']}"})
    for n in facts.get("news", {}).get("holdings", [])[:6]:
        matched = n.get("matched", [])
        feed.append({"tm": (n.get("published") or "")[-5:], "tag": "mk",
                     "hot": False, "matched": matched,
                     "url": n.get("link") or None, "source": n.get("source"),
                     "html": f"<b>{'・'.join(matched)}</b> {n['title']}"})

    out = {"as_of": facts.get("generated_at_jst", "")[:16].replace("T", " "),
           "holds": holds, "macro": macro, "tape": tape[:14], "feed": feed[:8],
           # 「開示/記事ゼロ」と「取得失敗」をHTML側で区別するためのステータス。
           # feedが空配列なだけでは両者を見分けられない。
           "tdnet_status": facts.get("tdnet", {}).get("status"),
           "news_status": facts.get("news", {}).get("status")}
    if sectors:
        out["sectors"] = sectors
    # LLM層(ai要約)が生成できた時だけ差し込むフック。未接続時はキー自体を出さない。
    # terminal_dashboard.html側は ai キーが無ければ「算出不可」と表示するのが正しい挙動。
    if facts.get("ai"):
        out["ai"] = facts["ai"]
    return out


def write(facts: dict, hist: dict, out_dir: pathlib.Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dashboard.json").write_text(
        json.dumps(build(facts, hist), ensure_ascii=False, indent=1), encoding="utf-8")
