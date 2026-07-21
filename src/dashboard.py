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
        })

    # TDnet高シグナルを flag に反映
    for i in facts.get("tdnet", {}).get("high_signal", []):
        for h in holds:
            if h["code"].startswith(i.get("code", "")):
                h["flag"] = "・".join(i["high_signal_words"][:2]) + " 開示"
                h["hot"] = True

    macro_keys = [("^SOX", "SOX 半導体指数"), ("^IXIC", "NASDAQ"), ("^GSPC", "S&P 500"),
                  ("^VIX", "VIX 恐怖指数"), ("JPY=X", "USD/JPY"), ("^N225", "日経225")]
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

    feed = []
    for i in facts.get("tdnet", {}).get("items", [])[:6]:
        feed.append({"tm": i["time"][-5:], "tag": "td",
                     "hot": bool(i.get("high_signal_words")),
                     "html": f"<b>{i['company']}</b> {i['title']}"})
    for n in facts.get("news", {}).get("holdings", [])[:6]:
        feed.append({"tm": (n.get("published") or "")[-5:], "tag": "mk",
                     "html": f"<b>{'・'.join(n.get('matched',[]))}</b> {n['title']}"})

    return {"as_of": facts.get("generated_at_jst", "")[:16].replace("T", " "),
            "holds": holds, "macro": macro, "tape": tape[:14], "feed": feed[:8]}


def write(facts: dict, hist: dict, out_dir: pathlib.Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dashboard.json").write_text(
        json.dumps(build(facts, hist), ensure_ascii=False, indent=1), encoding="utf-8")
