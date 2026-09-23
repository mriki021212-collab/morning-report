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


def _fx_block(facts: dict) -> tuple[dict | None, str | None]:
    """ドル円。値と status を別キーで返す。

    「取得できていない」と「値が0/空」を画面が取り違えないよう、status は必ず別に出す。
    status が None のときだけ fx に数値が入る。
    """
    f = facts.get("fx")
    if not f:
        return None, "取得できていません（fx ブロックが生成されていません）"
    if f.get("status"):
        return None, f["status"]
    r = f.get("rates") or {}
    h = f.get("hourly") or {}
    return {
        "code": f.get("code"), "nm": f.get("name"),
        "px": f.get("close"), "prev": f.get("prev_close"),
        "chgYen": f.get("chg_yen"), "chg": f.get("chg_pct"),
        "ma20": f.get("ma20"), "devMa20": f.get("dev_ma20_pct"),
        "high20": f.get("high_20d"), "low20": f.get("low_20d"),
        "asof": f.get("as_of"),
        "stale": bool(f.get("stale")),
        "warn": f.get("stale_warning") or f.get("prev_gap_warning"),
        # 当日(JST)付けの形成中の足。確定値ではないので px とは別キーで渡す。
        "forming": f.get("forming_bar"),
        # 前日の1時間足。取得できていなければ status だけを持つ（値は出さない）。
        "h1": ({"date": h.get("date_jst"), "high": h.get("high"), "low": h.get("low"),
                "bars": h.get("bars"), "note": h.get("mismatch_note")}
               if not h.get("status") else None),
        "h1Status": h.get("status"),
        # 日米金利差。片方でも欠ければ spread は None のまま status に理由が入る。
        "rates": {
            "us10y": (r.get("us10y") or {}).get("value_pct"),
            "us10yStatus": (r.get("us10y") or {}).get("status"),
            "jp10y": (r.get("jp10y") or {}).get("value_pct"),
            "jp10yStatus": (r.get("jp10y") or {}).get("status"),
            "spread": r.get("spread_pt"),
            "spreadStatus": r.get("spread_status"),
            # 値は出せるが基準日が古い/ズレている場合の注意書き（値と混ぜない）
            "spreadWarn": r.get("spread_warning"),
        },
    }, None


def _econ_block(facts: dict) -> tuple[dict | None, str | None]:
    """重要経済指標カレンダー。今日 / 今後 を分けて渡す。

    「未設定」「要更新」「本日は予定なし」は全部意味が違うので、
    空配列ひとつにまとめず status で区別できるようにする。
    """
    e = facts.get("econ_calendar")
    if not e:
        return None, "取得できていません（econ_calendar が生成されていません）"
    st = e.get("status")
    rows = lambda key: [{
        "date": x["date"], "time": x.get("time_jst"), "cc": x.get("country_label") or x.get("country"),
        "nm": x["name"], "imp": x["importance"], "impLabel": x.get("importance_label"),
        "fc": x.get("forecast"), "prev": x.get("previous"), "url": x.get("source_url"),
    } for x in (e.get(key) or [])]
    if st and st != "ok":
        # 未設定/要更新でも、登録済みの予定があるなら隠さずに出す
        return ({"today": rows("today"), "upcoming": rows("upcoming"),
                 "coverageUntil": e.get("coverage_until"),
                 "upcomingDays": e.get("upcoming_days"),
                 "nRegistered": e.get("n_registered"),
                 "gaps": e.get("gaps") or [],
                 "invalid": e.get("invalid") or []} if e.get("n_registered") else None), st
    return {
        "today": rows("today"), "upcoming": rows("upcoming"),
        "coverageUntil": e.get("coverage_until"),
        "upcomingDays": e.get("upcoming_days"),
        "nRegistered": e.get("n_registered"),
        "gaps": e.get("gaps") or [],
        "invalid": e.get("invalid") or [],
    }, None


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
            # warn: 値は出せるが、その終値の出どころが本来の引けではない行に付く
            #（fx で置き換えられなかったドル円）。HTML 側はこれをセルの説明に出す。
            macro.append({"k": label, "v": f"{m['close']:,.2f}", "c": m.get("chg_pct", 0),
                          "asof": m.get("as_of"), "warn": m.get("close_basis_warning")})

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

    # ドル円 / 重要経済指標。いずれも値と status を別キーで出す（既存キーには触らない）。
    fx_block, fx_status = _fx_block(facts)
    out["fx"] = fx_block
    out["fx_status"] = fx_status
    econ_block, econ_status = _econ_block(facts)
    out["econ_calendar"] = econ_block
    out["econ_status"] = econ_status

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
