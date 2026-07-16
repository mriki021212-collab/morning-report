"""
J-Quants API（JPX公式）: 信用倍率・空売り比率・投資部門別売買状況。
これらは yfinance では取得できない。Freeプランは12週遅延、Light(¥1,650/月)で前日分まで。
環境変数 JQ_REFRESH_TOKEN が無ければ全て「現時点では確認できない」を返す（捏造しない）。
"""
from __future__ import annotations
import os
import datetime as dt
import requests

BASE = "https://api.jquants.com/v1"


def _id_token() -> str | None:
    rt = os.getenv("JQ_REFRESH_TOKEN")
    if not rt:
        return None
    r = requests.post(f"{BASE}/token/auth_refresh", params={"refreshtoken": rt}, timeout=30)
    if r.status_code != 200:
        return None
    return r.json().get("idToken")


def _get(path: str, token: str, **params):
    r = requests.get(f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"},
                     params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def margin_and_short(codes: list[str]) -> dict:
    token = _id_token()
    if not token:
        return {c: {"status": "現時点では確認できない（J-Quants未設定）"} for c in codes}

    out = {}
    for code in codes:
        c4 = code.replace(".T", "")
        rec: dict = {}
        # --- 信用取引週末残高 → 信用倍率 ---
        try:
            data = _get("/markets/weekly_margin_interest", token, code=c4)
            rows = data.get("weekly_margin_interest", [])
            if rows:
                r = sorted(rows, key=lambda x: x["Date"])[-1]
                long_, short_ = float(r["LongMarginTradeVolume"]), float(r["ShortMarginTradeVolume"])
                rec["margin_date"] = r["Date"]
                rec["margin_long"] = long_
                rec["margin_short"] = short_
                rec["margin_ratio"] = round(long_ / short_, 2) if short_ else None
            else:
                rec["margin_status"] = "現時点では確認できない"
        except Exception as e:
            rec["margin_status"] = f"取得失敗: {e}"

        # --- 空売り比率（銘柄別の日次空売り残高報告） ---
        try:
            frm = (dt.date.today() - dt.timedelta(days=30)).isoformat()
            data = _get("/markets/short_selling_positions", token, code=c4, **{"from": frm})
            rows = data.get("short_selling_positions", [])
            rec["short_positions_reported"] = len(rows)
            if rows:
                r = sorted(rows, key=lambda x: x["DisclosedDate"])[-1]
                rec["short_latest_disclosed"] = r.get("DisclosedDate")
                rec["short_latest_holder"] = r.get("ShortSellerName")
                rec["short_latest_ratio_pct"] = r.get("ShortPositionsToSharesOutstandingRatio")
        except Exception as e:
            rec["short_status"] = f"取得失敗: {e}"

        out[code] = rec
    return out


def trades_spec() -> dict:
    """投資部門別売買状況（外国人・個人・機関の売買動向、週次）"""
    token = _id_token()
    if not token:
        return {"status": "現時点では確認できない（J-Quants未設定）"}
    try:
        frm = (dt.date.today() - dt.timedelta(days=45)).isoformat()
        data = _get("/markets/trades_spec", token, section="TSEPrime", **{"from": frm})
        rows = sorted(data.get("trades_spec", []), key=lambda x: x["PublishedDate"])
        return {"latest": rows[-2:] if rows else "現時点では確認できない"}
    except Exception as e:
        return {"status": f"取得失敗: {e}"}
