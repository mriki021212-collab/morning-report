"""
TDnet適時開示コレクタ（やのしんWEB-API経由）。

キオクシアの Viasat 訴訟評決(2026-07-17 13:00開示)を、当時のRSSキーワード方式は
取りこぼした。適時開示は一次情報であり、キーワードに依存せず銘柄コードで直接取る。

やのしんAPIは英字入りコード(285A)に対応。JPX公式TDnet APIは有料だがこちらは無料。
保有銘柄を1リクエストでまとめて取得できる。

取れなかった時は必ず status を返す。「開示ゼロ」と「取得失敗」を区別する。
"""
from __future__ import annotations
import datetime as dt
import requests

JST = dt.timezone(dt.timedelta(hours=9))
BASE = "https://webapi.yanoshin.jp/webapi/tdnet/list"

# 開示表題に含まれると影響度が高いと考えられる語（分類のみ。判断はしない）
HIGH_SIGNAL = ("訴訟", "評決", "判決", "賠償", "特許", "下方修正", "上方修正",
               "業績予想", "配当", "自己株式", "新株予約権", "第三者割当",
               "減損", "特別損失", "債務超過", "上場廃止", "TOB", "公開買付",
               "MBO", "株式分割", "併合", "後発事象", "災害", "行政処分")


def _codes_param(codes: list[str]) -> str:
    # "6740.T" -> "6740"、"285A.T" -> "285A"
    return "-".join(c.replace(".T", "") for c in codes)


def fetch(codes: list[str], hours: int = 48, limit: int = 50) -> dict:
    """
    保有銘柄の適時開示を取得。
    返り値: {"status": "ok"|..., "items": [...], "high_signal": [...]}
    """
    url = f"{BASE}/{_codes_param(codes)}.json?limit={limit}"
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "morning-report/1.0"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"status": f"取得失敗: {type(e).__name__}: {e}", "items": [], "high_signal": []}

    cutoff = dt.datetime.now(JST) - dt.timedelta(hours=hours)
    items = []
    for row in data.get("items", []):
        d = row.get("Tdnet", row)  # やのしんはItem内にTdnetネスト
        title = d.get("title", "")
        pub_raw = d.get("pubdate") or d.get("update") or ""
        when = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                when = dt.datetime.strptime(pub_raw.strip(), fmt)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=JST)
                break
            except (ValueError, AttributeError):
                continue
        if when and when < cutoff:
            continue
        hit = [w for w in HIGH_SIGNAL if w in title]
        item = {
            "code": d.get("company_code", "")[:4] if d.get("company_code") else "",
            "company": d.get("company_name", ""),
            "title": title,
            "url": d.get("document_url") or d.get("url", ""),
            "time": when.strftime("%Y-%m-%d %H:%M") if when else pub_raw,
            "high_signal_words": hit,
        }
        items.append(item)

    high = [i for i in items if i["high_signal_words"]]
    return {
        "status": "ok" if items else "直近に開示なし",
        "source": "やのしんTDnet WEB-API",
        "items": items,
        "high_signal": high,
    }
