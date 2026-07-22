"""
個別ニュース（B層）＝Googleニュース検索RSS ＋ マクロ地合い（C層）＝新聞社RSS。

【B層の方式変更 v16】
従来のニュースサイトRSS（Yahoo/みんかぶ/株探/Reuters）は 403/404 で軒並み死んでいた。
Googleニュースの検索RSS( news.google.com/rss/search?q=銘柄名 )は到達性が高く、
銘柄名で狙い撃ちできる。保有株・セクター各社の名前で個別に検索して集約する。
これによりキオクシアのViasat訴訟記事のような銘柄固有ニュースを確実に拾う。

【C層】新聞社の総合経済RSS（wor.jp経由で生存確認済みのものだけ）。

「取得失敗」と「該当なし」は必ず区別する。
"""
from __future__ import annotations
import datetime as dt
import urllib.parse
import feedparser

# B層: 銘柄 -> Googleニュース検索クエリ（正式名・別名でOR検索）
HOLDING_QUERIES = {
    "ジャパンディスプレイ": "ジャパンディスプレイ OR JDI",
    "任天堂": "任天堂 OR Nintendo",
    "キオクシア": "キオクシア OR Kioxia",
}
SECTOR_QUERIES = {
    "東京エレクトロン": "東京エレクトロン",
    "レーザーテック": "レーザーテック",
    "アドバンテスト": "アドバンテスト",
    "ディスコ": "ディスコ 半導体",
    "ルネサス": "ルネサス エレクトロニクス",
    "SCREEN": "SCREEN ホールディングス",
    "信越化学": "信越化学",
    "SUMCO": "SUMCO",
    "半導体セクター": "半導体 株",
}

GNEWS = "https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"


def _gnews(query: str, hours: int, per: int) -> list[dict]:
    url = GNEWS.format(q=urllib.parse.quote(query))
    try:
        feed = feedparser.parse(url)
        if getattr(feed, "status", 200) >= 400:
            return [{"_error": f"status {feed.status}", "_q": query}]
    except Exception as e:
        return [{"_error": f"{type(e).__name__}", "_q": query}]

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    out = []
    for e in feed.entries[:per * 4]:
        t = e.get("published_parsed") or e.get("updated_parsed")
        when = dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else None
        if when and when < cutoff:
            continue
        title = e.get("title", "").strip()
        if not _is_real_article(title):
            continue
        out.append({"title": _clean_title(title), "link": e.get("link", ""),
                    "source": (e.get("source", {}) or {}).get("title", "Google News"),
                    "published": when.isoformat()[:16] if when else "不明"})
        if len(out) >= per:
            break
    return out


# --- ノイズ除去 ---
# Googleニュースは記事以外に「株価情報ページ」「Google Newsまとめ」を混ぜてくる。
# これらは見出しとして無価値なので弾く。2026/7/22のレポートで大量混入していた。
_NOISE_PATTERNS = (
    "：株価・株式情報", ":株価・株式情報", "値動きの背景をAIが解説",
    "今の株価の理由は", "株価・株式情報（", "Comprehensive up-to-date",
)
_NOISE_TITLES = ("Google News", "Google ニュース", "")


def _is_real_article(title: str) -> bool:
    if title in _NOISE_TITLES:
        return False
    return not any(p in title for p in _NOISE_PATTERNS)


def _clean_title(title: str) -> str:
    # 末尾の " - 媒体名" を1回だけ落として簡潔にする（読みやすさ）
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        # 媒体名は概ね短い。長い場合は記事タイトルの一部なので残す
        if len(tail) <= 20:
            return head.strip()
    return title


def fetch(macro_urls: list[str], hours: int = 24, per_stock: int = 3) -> dict:
    """
    返り値: {"status", "holdings":[...], "sector":[...], "macro":[...], "errors":[...]}
    holdings/sector の各記事に "matched"（銘柄名）が付く。
    macro_urls は C層用の新聞社RSS（config.yaml の rss）。
    """
    holdings, sector, errors = [], [], []
    seen = set()

    # --- B層: Googleニュースで銘柄ごとに検索 ---
    for name, q in HOLDING_QUERIES.items():
        for a in _gnews(q, hours, per_stock):
            if a.get("_error"):
                errors.append({"query": a["_q"], "error": a["_error"]}); continue
            if a["title"] in seen:
                continue
            seen.add(a["title"]); holdings.append({**a, "matched": [name]})
    for name, q in SECTOR_QUERIES.items():
        for a in _gnews(q, hours, 2):
            if a.get("_error"):
                errors.append({"query": a["_q"], "error": a["_error"]}); continue
            if a["title"] in seen:
                continue
            seen.add(a["title"]); sector.append({**a, "matched": [name]})

    # --- C層: 新聞社の総合経済RSS ---
    MACRO_KW = ["日経平均", "TOPIX", "半導体", "SOX", "NVIDIA", "エヌビディア", "AI",
                "FOMC", "FRB", "利上げ", "利下げ", "日銀", "為替", "円安", "円高",
                "米国株", "ナスダック", "メモリ", "TSMC", "株価", "相場", "金利"]
    macro = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    for u in macro_urls:
        try:
            feed = feedparser.parse(u)
            if getattr(feed, "status", 200) >= 400:
                errors.append({"url": u, "status": feed.status}); continue
        except Exception as e:
            errors.append({"url": u, "error": f"{type(e).__name__}"}); continue
        for e in feed.entries:
            t = e.get("published_parsed") or e.get("updated_parsed")
            when = dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else None
            if when and when < cutoff:
                continue
            title = e.get("title", "")
            text = title + " " + e.get("summary", "")
            if title in seen or not any(k in text for k in MACRO_KW):
                continue
            seen.add(title)
            macro.append({"title": title, "link": e.get("link", ""),
                          "source": feed.feed.get("title", u),
                          "published": when.isoformat()[:16] if when else "不明"})

    for lst in (holdings, sector, macro):
        lst.sort(key=lambda x: x.get("published", ""), reverse=True)

    got_any = holdings or sector or macro
    status = "ok" if got_any else ("全ソース取得失敗" if errors else "該当なし")
    return {"status": status, "holdings": holdings, "sector": sector,
            "macro": macro[:10], "errors": errors}
