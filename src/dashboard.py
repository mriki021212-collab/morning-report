"""
ダッシュボード用 JSON を書き出す。terminal_dashboard.html がこれを読んで実データを表示する。

このモジュールは facts に無い値を作らない。埋められないものはキーを出さないか、
status を付けて出す。HTML側はキーの有無/statusを見て「0件」と「取得失敗」を区別する。
"""
from __future__ import annotations
import datetime as dt
import json
import pathlib

JST = dt.timezone(dt.timedelta(hours=9))


def _spark_from(df, n=40):
    if df is None or df.empty:
        return []
    return [round(float(x), 2) for x in df["Close"].tail(n).tolist()]


def _chart_hist(df, n=260):
    """中央チャート用: 実際の取引日付+終値+出来高を最大n営業日ぶん返す。期間ボタンはこの
    配列をクライアント側でスライスするだけで、存在しない期間のデータを作り出さない。

    キー:
      d = 取引日 (YYYY-MM-DD)
      c = 終値。auto_adjust=False で取得しているため配当は未調整（素の終値）。
          2026-08-26 の実測では 7974.T で Close と Adj Close が 260日中158日ずれる。
          騰落率を出す側はこの前提をUIに明記すること。
      v = 出来高。取れなかった場合はキーごと出さない（0で埋めない）。
          d/c と同じ長さであることを保証し、長さが違えば v を出さない。
    """
    if df is None or df.empty:
        return {"d": [], "c": []}
    tail = df.tail(n)
    out = {
        "d": [ts.strftime("%Y-%m-%d") for ts in tail.index],
        "c": [round(float(x), 2) for x in tail["Close"].tolist()],
    }
    if "Volume" in tail.columns:
        vols = tail["Volume"].tolist()
        # NaN を 0 に潰さない。出来高が欠けた足がある系列は v ごと出さず、
        # HTML側は「出来高データがありません」でサブチャートを出さない挙動になる。
        if len(vols) == len(out["d"]) and not any(v != v or v is None for v in vols):
            out["v"] = [int(v) for v in vols]
    return out


def _rows(group: dict, hist: dict) -> list[dict]:
    """holds / watch 共通の1行。キー名と意味は従来の holds[] と完全に同じ。"""
    rows = []
    for code, s in (group or {}).items():
        if s.get("status"):
            continue
        h = _chart_hist(hist.get(code))
        rows.append({
            "code": code, "nm": s["name"], "px": s["close"], "base": s["close"],
            "chg": s["chg_pct"], "baseChg0": s["chg_pct"],
            "rsi": s["rsi14"], "ma25": s["dev_ma25_pct"],
            "vol": f"{s['volume']/1e6:.1f}M" if s.get("volume") else "—",
            "candle": s.get("candle", "—"),
            "flag": None,  # 高シグナル開示があれば下で差し込む
            "s": _spark_from(hist.get(code)),
            "hist": h,
            # 鮮度判定用。px / s / hist はすべてこの日の確定足に由来する。
            # HTML側はこの日付を直近営業日と突き合わせて「古い可能性」を出す。
            "asof": s.get("as_of") or (h["d"][-1] if h["d"] else None),
        })
    return rows


def _apply_tdnet_flags(facts: dict, *rowsets: list[dict]) -> None:
    for i in facts.get("tdnet", {}).get("high_signal", []):
        for rows in rowsets:
            for h in rows:
                if h["code"].startswith(i.get("code", "")):
                    h["flag"] = "・".join(i["high_signal_words"][:2]) + " 開示"
                    h["hot"] = True


def _mean_chg(snaps: dict) -> tuple[float | None, int]:
    """構成銘柄の前日比(%)の単純平均。取得できた銘柄のみで平均する（加重ではない）。"""
    vals = [s["chg_pct"] for s in (snaps or {}).values()
            if not s.get("status") and s.get("chg_pct") is not None]
    if not vals:
        return None, 0
    return round(sum(vals) / len(vals), 4), len(vals)


def build(facts: dict, hist: dict) -> dict:
    holds = _rows(facts.get("holdings", {}), hist)
    watch = _rows(facts.get("watch", {}), hist)
    _apply_tdnet_flags(facts, holds, watch)

    # config.yaml の macro: に登録されている全指標を出力する。
    # ここで一部だけに絞ると「取得済みなのに表示されない」欠損を自作することになる。
    macro_keys = [
        ("^N225", "日経225"), ("998405.T", "TOPIX"), ("^SOX", "SOX 半導体指数"),
        ("^IXIC", "NASDAQ"), ("^GSPC", "S&P 500"), ("^DJI", "NYダウ"),
        ("^VIX", "VIX 恐怖指数"), ("JPY=X", "USD/JPY"), ("^TNX", "米10年債利回り"),
        ("CL=F", "WTI原油"), ("GC=F", "金(GOLD)"), ("NIY=F", "日経平均先物(CME円建)"),
    ]
    macro = []
    for code, label in macro_keys:
        m = facts.get("macro", {}).get(code, {})
        if m.get("close") is not None:
            macro.append({"k": label, "v": f"{m['close']:,.2f}", "c": m.get("chg_pct", 0),
                          "asof": m.get("as_of")})

    tape = []
    for code, s in facts.get("sector", {}).items():
        if not s.get("status"):
            tape.append([f"{code.replace('.T','')} {s['name']}", s["close"], s["chg_pct"]])
    for g in (facts.get("sector_groups") or {}).values():
        for code, s in (g.get("members") or {}).items():
            if not s.get("status"):
                tape.append([f"{code.replace('.T','')} {s['name']}", s["close"], s["chg_pct"]])
    for code, s in facts.get("overseas_semis", {}).items():
        if not s.get("status"):
            tape.append([code, s["close"], s["chg_pct"]])

    # セクター騰落率。定義: sum(chg_pct) / 取得できた構成銘柄数（単純平均・加重ではない）。
    # sectors は「セクター名 -> 数値」のまま変更しない（HTML側の既存解釈を壊さないため）。
    # 構成銘柄名と算出方法は sector_defs に別キーとして持たせる。
    sectors: dict[str, float] = {}
    sector_defs: dict[str, dict] = {}

    def _add_sector(name: str, snaps: dict) -> None:
        avg, n = _mean_chg(snaps)
        members = [{"code": c, "nm": s.get("name"),
                    "chg": None if s.get("status") else s.get("chg_pct"),
                    "status": s.get("status")}
                   for c, s in (snaps or {}).items()]
        sector_defs[name] = {
            "members": members,
            "n_used": n,
            "n_total": len(members),
            "method": "構成銘柄の前日比(%)の単純平均（加重ではない）",
        }
        if avg is not None:
            sectors[name] = avg

    _add_sector("半導体", facts.get("sector", {}))
    for gname, g in (facts.get("sector_groups") or {}).items():
        _add_sector(gname, g.get("members", {}))

    # 投資信託。株式とは別セクション。RSI/移動平均乖離などのテクニカルは持たない
    # （日次1本値しか無く、株式と同じ指標を当てても意味が異なるため）。
    funds = []
    for code, f in (facts.get("funds") or {}).items():
        funds.append({
            "code": code,
            "nm": f.get("name"),
            "nav": f.get("nav"),
            "chg": f.get("chg"),
            "chgPct": f.get("chg_pct"),
            "asof": f.get("as_of"),
            "status": f.get("status"),   # 取得失敗はここに入る。値では隠さない
        })

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

    # 価格系列の基準日。holds/watch の確定足のうち最も新しい日付。
    # HTML側はこれと直近営業日を比べて「データが古い可能性」を出す。
    asofs = [r["asof"] for r in (holds + watch) if r.get("asof")]

    out = {"as_of": facts.get("generated_at_jst", "")[:16].replace("T", " "),
           # 生成時刻をISOのまま持つ。上の as_of は表示用に切り詰めてあり、
           # タイムスタンプ比較には使えない（日付一致判定ではなく時刻比較をするため）。
           "generated_at_jst": facts.get("generated_at_jst", ""),
           "market_asof": max(asofs) if asofs else None,
           "session": facts.get("session"),
           "holds": holds, "watch": watch,
           "macro": macro, "tape": tape[:18], "feed": feed[:8],
           # 「開示/記事ゼロ」と「取得失敗」をHTML側で区別するためのステータス。
           # feedが空配列なだけでは両者を見分けられない。
           "tdnet_status": facts.get("tdnet", {}).get("status"),
           "news_status": facts.get("news", {}).get("status")}
    if sectors:
        out["sectors"] = sectors
    if sector_defs:
        out["sector_defs"] = sector_defs
    if funds:
        out["funds"] = funds
    # LLM層(ai要約)が生成できた時だけ差し込むフック。未接続時はキー自体を出さない。
    # terminal_dashboard.html側は ai キーが無ければパネルごと出さないのが正しい挙動。
    if facts.get("ai"):
        out["ai"] = facts["ai"]
    return out


def write(facts: dict, hist: dict, out_dir: pathlib.Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dashboard.json").write_text(
        json.dumps(build(facts, hist), ensure_ascii=False, indent=1), encoding="utf-8")
