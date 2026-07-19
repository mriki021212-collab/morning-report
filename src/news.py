"""
個別ニュース（B層）＋マクロ地合い（C層）のRSS収集。

旧版はタイトルに固定キーワードが含まれるかだけを見ており、キオクシアのViasat訴訟
(「Viasat」「特許」を含む見出し)を取りこぼした。原因は2つ:
  1. マッチ対象がタイトルのみ（本文・説明を見ていない）
  2. 銘柄ごとの別名（英語名・製品名・略称）を持っていなかった

対策: 銘柄ごとに「コード/正式名/別名」の束を持ち、タイトル+説明の全体に対して
どれか1つでも当たれば拾う。当たった銘柄名も一緒に返す。
"""
from __future__ import annotations
import datetime as dt
import feedparser

# 銘柄 -> マッチに使う語の束（英語名・製品名・略称・関連固有名詞を含む）
HOLDING_ALIASES = {
    "6740": ["ジャパンディスプレイ", "JDI", "Japan Display"],
    "7974": ["任天堂", "Nintendo", "スイッチ", "Switch"],
    "285A": ["キオクシア", "Kioxia", "Viasat", "NAND", "フラッシュメモリ"],
}

# 半導体セクター（B層の拡張）
SECTOR_ALIASES = {
    "8035": ["東京エレクトロン", "東エレク", "Tokyo Electron", "TEL"],
    "6920": ["レーザーテック", "Lasertec"],
    "6146": ["ディスコ", "DISCO"],
    "6857": ["アドバンテスト", "Advantest"],
    "7735": ["SCREEN", "スクリーン"],
    "6723": ["ルネサス", "Renesas"],
    "6526": ["ソシオネクスト", "Socionext"],
    "4063": ["信越化学", "信越"],
    "3436": ["SUMCO"],
}

# C層: 地合い・マクロのキーワード（個別銘柄に紐づかない全体ニュース）
MACRO_KEYWORDS = [
    "日経平均", "TOPIX", "半導体", "SOX", "エヌビディア", "NVIDIA", "AI相場",
    "FOMC", "FRB", "利上げ", "利下げ", "日銀", "為替", "円安", "円高", "ドル円",
    "米国株", "ナスダック", "HBM", "メモリ", "TSMC", "マイクロン", "SKハイニックス",
]


def _entry_text(e) -> str:
    return " ".join(filter(None, [e.get("title", ""), e.get("summary", ""), e.get("description", "")]))


def _when(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else None


def fetch(urls: list[str], hours: int = 24, limit: int = 40) -> dict:
    """
    返り値: {"status", "holdings": [...], "sector": [...], "macro": [...], "errors": [...]}
    holdings/sector の各記事には "matched"（当たった銘柄名リスト）が付く。
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    holdings, sector, macro, errors = [], [], [], []
    seen = set()

    for u in urls:
        try:
            feed = feedparser.parse(u)
            if getattr(feed, "status", 200) >= 400:
                errors.append({"url": u, "status": getattr(feed, "status", "?")})
                continue
        except Exception as e:
            errors.append({"url": u, "error": f"{type(e).__name__}: {e}"})
            continue

        for e in feed.entries:
            when = _when(e)
            if when and when < cutoff:
                continue
            title = e.get("title", "")
            if title in seen:
                continue
            text = _entry_text(e)
            link = e.get("link", "")
            src = feed.feed.get("title", u)
            base = {"title": title, "link": link, "source": src,
                    "published": when.isoformat()[:16] if when else "不明"}

            h_hit = [nm for code, al in HOLDING_ALIASES.items()
                     for nm in [al[0]] if any(a in text for a in al)]
            s_hit = [al[0] for code, al in SECTOR_ALIASES.items() if any(a in text for a in al)]

            if h_hit:
                holdings.append({**base, "matched": h_hit}); seen.add(title)
            elif s_hit:
                sector.append({**base, "matched": s_hit}); seen.add(title)
            elif any(k in text for k in MACRO_KEYWORDS):
                macro.append(base); seen.add(title)

    for lst in (holdings, sector, macro):
        lst.sort(key=lambda x: x.get("published", ""), reverse=True)

    status = "ok"
    if errors and not (holdings or sector or macro):
        status = "全ソース取得失敗（下記errors参照）"
    return {"status": status,
            "holdings": holdings[:limit], "sector": sector[:limit],
            "macro": macro[:limit], "errors": errors}
