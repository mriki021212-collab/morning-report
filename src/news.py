from __future__ import annotations
import datetime as dt
import feedparser

KEYWORDS = [
    "ジャパンディスプレイ", "JDI", "任天堂", "Nintendo", "Switch",
    "キオクシア", "Kioxia", "NAND", "半導体", "SOX", "エヌビディア", "NVIDIA",
    "TSMC", "マイクロン", "サムスン", "SKハイニックス", "ASML", "HBM",
    "東京エレクトロン", "レーザーテック", "ディスコ", "アドバンテスト",
    "SCREEN", "ルネサス", "ソシオネクスト", "信越化学", "SUMCO",
    "日銀", "FOMC", "FRB", "為替", "円安", "円高", "日経平均",
]


def fetch(urls: list[str], hours: int = 24, limit: int = 40) -> list[dict]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    out = []
    for u in urls:
        try:
            feed = feedparser.parse(u)
        except Exception as e:  # ネットワーク失敗はサイレントに落とさず記録
            out.append({"source": u, "error": str(e)})
            continue
        for e in feed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            when = dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else None
            if when and when < cutoff:
                continue
            title = e.get("title", "")
            if not any(k in title for k in KEYWORDS):
                continue
            out.append({
                "title": title,
                "link": e.get("link"),
                "published": when.isoformat() if when else "不明",
                "source": feed.feed.get("title", u),
            })
    out.sort(key=lambda x: x.get("published", ""), reverse=True)
    return out[:limit]
