"""
⑪ 株価インパクトニュース。

ただのニュース一覧ではなく「株価を実際に大きく動かした / 今日動かしうる」材料だけを
毎朝最大5件出す。点数はすべてルール計算（重みは config.yaml の impact:）。
LLM は予測欄の文章を書くだけで、点数にも事実欄にも触らない。

【入力】
  - 既存の取得結果をそのまま再利用する（再取得しない）
      facts["tdnet"]          … A層 TDnet（やのしんWEB-API・保有+ウォッチの銘柄コード指定）
      facts["news"]           … ⑥-B Googleニュース検索 / ⑥-C wor.jp 経由の新聞社RSS
      facts["macro"] ほか      … 市場の反応の判定に使う前日比
      facts["econ_calendar"]  … 「これから効く」の予定（config.yaml の手動管理分）
      earnings（out/earnings.json と同じ dict）… 「これから効く」の決算予定
  - このモジュール自身が追加で取るソースは SOURCES に列挙したものだけ。
    採否は docs/impact_sources.md に実測値つきで記録してある（`python src/impact.py --probe`
    で再測定できる）。説明文ではなく、実際に叩いた結果だけで採否を決めている。

【状態の区別】（このプロジェクトの絶対ルール）
  impact_status = "ok"     … 60点以上が1件以上
                  "empty"  … ソースは取れたが60点以上が0件 →「本日は大きな材料なし」
                  "failed" … 使えるソースが1つも取れなかった →「取得できていません」
  一部ソースだけ落ちた場合は ok/empty のまま source_errors に残す（黙って消さない）。

【表現ルール】
  出力に「買い」「売り」「推奨」「狙い目」等を含めない。方向は「上昇要因/下落要因」だけ。
  見出しに含まれる場合は言い換え、その見出しには「※表記調整」を付ける（原文として装わない）。
  記事本文は取得も転載もしない。見出し・自前の短い要約・URLのみ。
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests

import news  # ノイズ判定（株価情報ページ等）を ⑥-B と揃える

JST = dt.timezone(dt.timedelta(hours=9))
UTC = dt.timezone.utc
ROOT = pathlib.Path(__file__).resolve().parents[1]
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) morning-report/1.0"}
GNEWS = "https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja"

PREDICTION_NOTE = "予測であり保証ではありません"
LAYER_RANK = {"A": 0, "B": 1, "C": 2, "other": 3}
COMPANY_EVENTS = {"scandal", "earnings_revision", "ma_buyback", "scheduled_earnings"}

# ---------------------------------------------------------------------------
# ソース。ここにあるのは docs/impact_sources.md で「採用」になったものだけ。
#   kind="rss"   … そのURLのRSS/RDF
#   kind="gnews" … Googleニュース検索RSS。q に検索語。媒体名は記事の <source> から取る
#   naive_tz     … 日時にタイムゾーンが無い/解釈できない表記のときの補正
# ---------------------------------------------------------------------------
SOURCES = [
    # A層（一次情報）
    {"id": "boj", "layer": "A", "media": "日本銀行", "kind": "rss",
     "url": "https://www.boj.or.jp/rss/whatsnew.xml"},
    {"id": "frb", "layer": "A", "media": "FRB", "kind": "rss",
     "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"id": "mof", "layer": "A", "media": "財務省", "kind": "rss",
     "url": "https://www.mof.go.jp/news.rss"},
    # 金融庁の pubDate は "Tue, 10 Nov 2026 10:00:00 JST" 表記で、feedparser は JST を
    # 解釈できず UTC として扱う（実測で9時間未来にずれた）。生文字列から読み直す。
    {"id": "fsa", "layer": "A", "media": "金融庁", "kind": "rss", "naive_tz": "JST",
     "url": "https://www.fsa.go.jp/fsaNewsListAll_rss2.xml"},
    # B層（主要報道）
    {"id": "nhk", "layer": "B", "media": "NHK", "kind": "rss",
     "url": "https://www.nhk.or.jp/rss/news/cat5.xml"},
    {"id": "nikkei_markets", "layer": "B", "media": "日本経済新聞", "kind": "rss",
     "url": "https://assets.wor.jp/rss/rdf/nikkei/markets.rdf"},
    # ロイター日本語: wor.jp は 403。Googleニュースの site: 指定で代替
    {"id": "reuters", "layer": "B", "media": "ロイター", "kind": "gnews",
     "q": "site:jp.reuters.com 株"},
    # ブルームバーグ日本語: wor.jp は 403、site:bloomberg.co.jp は最新が2016年。
    # 現行の日本語版は bloomberg.com/jp 配下（実測で当日分が返る）
    {"id": "bloomberg", "layer": "B", "media": "ブルームバーグ", "kind": "gnews",
     "q": "site:bloomberg.com/jp 株"},
    # C層（市況速報）
    {"id": "fisco_kyojaku", "layer": "C", "media": "フィスコ（強弱材料）", "kind": "gnews",
     "q": "強弱材料 フィスコ"},
    {"id": "fisco_yoritsuki", "layer": "C", "media": "フィスコ（寄り付き概況）", "kind": "gnews",
     "q": "寄り付き概況 フィスコ"},
    {"id": "investing", "layer": "C", "media": "Investing.com日本版", "kind": "rss",
     "url": "https://jp.investing.com/rss/news.rss"},
    # 株探: 自前RSSは 404。Googleニュースの site: 指定で代替
    {"id": "kabutan", "layer": "C", "media": "株探", "kind": "gnews",
     "q": "site:kabutan.jp 株価"},
    # 市況まとめ（媒体を限定しない）。大きな相場変動を複数媒体で確認するための枠。
    # 層は記事ごとの媒体名で決まる（ここで指定する layer は使われない）。
    {"id": "gn_market_jp", "layer": "other", "media": "Googleニュース", "kind": "gnews",
     "q": "日経平均 大引け"},
    {"id": "gn_market_us", "layer": "other", "media": "Googleニュース", "kind": "gnews",
     "q": "米国株 ナスダック"},
]

# 見出し中の明示的なクレジット（配信元）→ (層, 配信元)。ポータル(Yahoo!等)経由の転載でも
# 配信元で数える（同じ記事の転載を「別媒体で確認」と数えないため）。
CREDIT_LAYERS = [
    ("(フィスコ)", "C", "フィスコ"), ("（フィスコ）", "C", "フィスコ"), ("執筆 Fisco", "C", "フィスコ"),
    ("(株探ニュース)", "C", "株探"), ("[株探]", "C", "株探"),
    ("（ロイター）", "B", "ロイター"), ("(ロイター)", "B", "ロイター"),
    ("(Bloomberg)", "B", "ブルームバーグ"), ("（ブルームバーグ）", "B", "ブルームバーグ"),
    ("（日本経済新聞）", "B", "日本経済新聞"), ("（NHK）", "B", "NHK"),
]
# 媒体名の表記ゆれ → 正規名（独立したソースの数え上げにだけ使う）
OUTLET_CANON = [
    ("ロイター", "ロイター"), ("Reuters", "ロイター"), ("ブルームバーグ", "ブルームバーグ"),
    ("Bloomberg", "ブルームバーグ"), ("日本経済新聞", "日本経済新聞"), ("日経", "日本経済新聞"),
    ("NHK", "NHK"), ("産経", "産経新聞"), ("読売", "読売新聞"), ("フィスコ", "フィスコ"),
    ("FISCO", "フィスコ"), ("株探", "株探"), ("kabutan", "株探"), ("財経新聞", "財経新聞"),
    ("Investing.com", "Investing.com"),
]


def outlet_of(media: str) -> str:
    return next((c for k, c in OUTLET_CANON if k in (media or "")), media or "")

# 媒体名（Googleニュースの <source> 等）→ 層。ここに無い媒体は "other"（件数には数えるが、
# 「異なる層で確認」の加点には使わない）。部分一致で判定する。
PUBLISHER_LAYERS = [
    ("日本経済新聞", "B"), ("日経", "B"), ("NHK", "B"), ("ロイター", "B"), ("Reuters", "B"),
    ("ブルームバーグ", "B"), ("Bloomberg", "B"), ("産経", "B"), ("読売", "B"),
    ("フィスコ", "C"), ("FISCO", "C"), ("財経新聞", "C"), ("Investing.com", "C"),
    ("株探", "C"), ("かぶたん", "C"), ("kabutan", "C"),
]

# 禁止表現 → 言い換え。長いものから順に置換する（「売り上げ」を「売却上げ」にしないため）。
BANNED_REWRITE = [
    ("売り上げ", "売上"), ("買い時", "購入時期"), ("売り時", "売却時期"),
    ("買い手", "購入者"), ("売り手", "売却側"), ("買い戻し", "買戻し"), ("売り越し", "売越し"),
    ("買い越し", "買越し"), ("売り込", "売込"), ("買い場", "購入機会"),
    ("狙い目", "注目点"), ("推奨", "評価"), ("買い", "買付"), ("売り", "売却"),
]
BANNED_WORDS = ("買い", "売り", "推奨", "狙い目", "買い時", "今が買い時")


# ============================================================================
# 小物
# ============================================================================
def _now() -> dt.datetime:
    return dt.datetime.now(JST)


def is_trading_day(d: dt.date) -> bool:
    # main.is_trading_day と同じ定義（main を import すると循環するため複製）
    import jpholiday
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    return (d.month, d.day) not in [(1, 1), (1, 2), (1, 3), (12, 31)]


def window_start(now: dt.datetime) -> dt.datetime:
    """前営業日の東証大引け 15:30 JST。now が当日15:30以降なら当日の15:30。"""
    d = now.date()
    close_today = dt.datetime.combine(d, dt.time(15, 30), JST)
    if is_trading_day(d) and now >= close_today:
        return close_today
    d -= dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return dt.datetime.combine(d, dt.time(15, 30), JST)


def sanitize(text: str) -> tuple[str, bool]:
    """禁止表現を言い換える。変えたかどうかも返す（変えたら原文と装わない）。"""
    out = text or ""
    for a, b in BANNED_REWRITE:
        out = out.replace(a, b)
    return out, out != (text or "")


def contains_banned(text: str) -> list[str]:
    return [w for w in BANNED_WORDS if w in (text or "")]


def _clean_title(title: str) -> str:
    title = (title or "").strip()
    if " - " in title:
        head, _, tail = title.rpartition(" - ")
        if len(tail) <= 25:
            return head.strip()
    return title


def _norm(s: str) -> str:
    # 配信元クレジット（「（ロイター）」等）は同一記事の判定の邪魔になるので落とす
    for k, *_ in CREDIT_LAYERS:
        s = (s or "").replace(k, "")
    s = re.sub(r"[\s　「」『』【】（）()［］\[\]、。,.・:：!！?？\-－—…\"'|｜]", "", s or "")
    return s.lower()


def _bigrams(s: str) -> set[str]:
    s = _norm(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def similarity(a: str, b: str) -> float:
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def publisher_layer(name: str) -> str:
    for key, layer in PUBLISHER_LAYERS:
        if key in (name or ""):
            return layer
    return "other"


def _parse_when(entry, naive_tz: str | None = None) -> dt.datetime | None:
    if naive_tz == "JST":
        raw = entry.get("published") or entry.get("updated") or ""
        m = re.search(r"(\d{1,2} \w{3} \d{4} \d{2}:\d{2}:\d{2})", raw)
        if m:
            try:
                return dt.datetime.strptime(m.group(1), "%d %b %Y %H:%M:%S").replace(tzinfo=JST)
            except ValueError:
                pass
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return dt.datetime(*t[:6], tzinfo=UTC) if t else None


# ============================================================================
# 取得
# ============================================================================
def source_url(src: dict, after: dt.date | None = None, before: dt.date | None = None) -> str:
    if src["kind"] == "gnews":
        q = src["q"]
        if after and before:
            q += f" after:{after:%Y-%m-%d} before:{before:%Y-%m-%d}"
        return GNEWS.format(q=urllib.parse.quote(q))
    return src["url"]


def fetch_raw(url: str, timeout: float) -> tuple[int | None, bytes | None, str | None]:
    """(HTTPステータス, 本文, エラー)。タイムアウト・例外はエラー文字列で返す。"""
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        return r.status_code, r.content, None if r.status_code < 400 else f"HTTP {r.status_code}"
    except Exception as e:
        return None, None, f"{type(e).__name__}"


def parse_feed(src: dict, body: bytes) -> list[dict]:
    feed = feedparser.parse(body)
    items = []
    for e in feed.entries:
        title = _clean_title(e.get("title", ""))
        # 株価情報ページ等の記事でないもの（news.py と同じ判定）
        if not title or not news._is_real_article(title):
            continue
        raw_title = e.get("title", "")
        if src["kind"] == "gnews":
            media = ((e.get("source") or {}).get("title") or src["media"]).strip()
            # site: 指定のクエリでも媒体名は記事側の <source> を正とする
            layer = publisher_layer(media) if publisher_layer(media) != "other" else src["layer"]
            outlet = outlet_of(media)
            credit = next(((lv, nm) for k, lv, nm in CREDIT_LAYERS if k in raw_title), None)
            if credit:
                outlet = credit[1]
                if layer == "other":
                    layer = credit[0]
        else:
            media, layer, outlet = src["media"], src["layer"], outlet_of(src["media"])
        items.append({
            "title": title, "link": e.get("link", ""), "media": media, "layer": layer,
            "outlet": outlet, "when": _parse_when(e, src.get("naive_tz")), "source_id": src["id"],
        })
    return items


def fetch_sources(sources: list[dict], timeout: float, fetcher=None,
                  after: dt.date | None = None, before: dt.date | None = None) -> tuple[list, list]:
    """全ソースを並列取得。返り値 (記事リスト, ソース毎の結果)。
    fetcher(url, timeout) を差し替えるとテスト用フィクスチャ/故障注入ができる。"""
    fetcher = fetcher or fetch_raw
    items, results = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetcher, source_url(s, after, before), timeout): s for s in sources}
        for f in as_completed(futs):
            s = futs[f]
            try:
                status, body, err = f.result()
            except Exception as e:  # fetcher 自体の異常も失敗として扱う
                status, body, err = None, None, type(e).__name__
            rec = {"id": s["id"], "media": s["media"], "layer": s["layer"],
                   "http": status, "error": err, "n": 0}
            if body is not None and not err:
                try:
                    got = parse_feed(s, body)
                    rec["n"] = len(got)
                    items += got
                except Exception as e:
                    rec["error"] = f"parse: {type(e).__name__}"
            results.append(rec)
    results.sort(key=lambda r: r["id"])
    return items, results


def reuse_existing(facts: dict) -> tuple[list, list]:
    """既存の ⑥-A/⑥-B/⑥-C の取得結果を記事リストに変換する（再取得しない）。"""
    items, results = [], []
    td = facts.get("tdnet") or {}
    td_failed = str(td.get("status", "")).startswith("取得失敗") or not td
    results.append({"id": "tdnet(既存⑥-A)", "media": "TDnet", "layer": "A", "http": None,
                    "error": (td.get("status") or "未取得") if td_failed else None,
                    "n": len(td.get("items") or [])})
    for i in td.get("items") or []:
        when = None
        try:
            when = dt.datetime.strptime(i.get("time", ""), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except ValueError:
            pass
        items.append({"title": f"{i.get('company', '')} {i.get('title', '')}".strip(),
                      "link": i.get("url", ""), "media": "TDnet", "layer": "A", "when": when,
                      "source_id": "tdnet", "code": i.get("code")})
    nw = facts.get("news") or {}
    nw_failed = str(nw.get("status", "")).startswith("全ソース取得失敗") or not nw
    n = 0
    for key in ("holdings", "sector", "macro"):
        for a in nw.get(key) or []:
            when = None
            try:
                when = dt.datetime.fromisoformat(a.get("published", "")).replace(tzinfo=UTC)
            except ValueError:
                pass
            media = a.get("source", "")
            # ⑥-C は wor.jp 経由の新聞社RSS。フィード名(例「日経 経済」)から層を引く
            items.append({"title": _clean_title(a.get("title", "")), "link": a.get("link", ""),
                          "media": media, "layer": publisher_layer(media), "when": when,
                          "source_id": f"news.{key}"})
            n += 1
    results.append({"id": "news(既存⑥-B/⑥-C)", "media": "Googleニュース/wor.jp", "layer": "B",
                    "http": None, "error": (nw.get("status") or "未取得") if nw_failed else None,
                    "n": n})
    return items, results


# ============================================================================
# 分類・点数
# ============================================================================
def classify(title: str, cfg_imp: dict) -> list[str]:
    hits = []
    for cat, words in (cfg_imp.get("keywords") or {}).items():
        if any(w in title for w in words):
            hits.append(cat)
    return hits


def focus_names(cfg: dict) -> dict[str, list[str]]:
    """{ticker: [名前, 別名...]}。保有・ウォッチ・半導体セクター・電線・海外半導体。"""
    out: dict[str, list[str]] = {}
    groups = [cfg.get("holdings"), cfg.get("watch"), cfg.get("sector"), cfg.get("overseas_semis")]
    for g in (cfg.get("sector_groups") or {}).values():
        groups.append(g.get("members"))
    for g in groups:
        for it in g or []:
            out.setdefault(it["code"], []).append(it["name"])
    for code, aliases in ((cfg.get("impact") or {}).get("aliases") or {}).items():
        out.setdefault(code, []).extend(aliases)
    return out


def _snap(facts: dict, code: str) -> dict | None:
    for g in ("macro", "holdings", "watch", "sector", "overseas_semis"):
        s = (facts.get(g) or {}).get(code)
        if s:
            return s
    for g in (facts.get("sector_groups") or {}).values():
        s = (g.get("members") or {}).get(code)
        if s:
            return s
    return None


def _usable(s: dict | None, now: dt.datetime, max_age_days: int) -> bool:
    """前日比が使える状態か。status付き・値なし・古すぎる値は使わない（古い値は値ではない）。"""
    if not s or s.get("status") or not isinstance(s.get("chg_pct"), (int, float)):
        return False
    try:
        d = dt.date.fromisoformat(str(s.get("as_of"))[:10])
    except ValueError:
        return False
    return (now.date() - d).days <= max_age_days


def market_reaction(title: str, cats: list[str], related: list[str], facts: dict,
                    cfg_imp: dict, now: dt.datetime) -> tuple[int, list[dict], dict | None]:
    """(加点, 判定に使った指標の一覧, ログ用の代表指標)。指標は facts の実測前日比のみ。"""
    w = cfg_imp["weights"]
    th = cfg_imp["thresholds"]
    max_age = th.get("max_age_days", 4)
    used, pts, key_ind = [], 0, None

    # 1) 関連個別株（米国株・ADR / 前日の東証）。銘柄名が見出しに出たものだけを関連とする
    for code in related:
        s = _snap(facts, code)
        if not _usable(s, now, max_age):
            continue
        row = {"code": code, "name": s.get("name"), "chg_pct": round(s["chg_pct"], 2),
               "as_of": s.get("as_of")}
        used.append(row)
        if abs(s["chg_pct"]) >= th["stock_pct"]:
            pts = max(pts, w["reaction_stock"])
            if key_ind is None or abs(row["chg_pct"]) > abs(key_ind["chg_pct"]):
                key_ind = row

    # 2) 指数（CME日経先物・米国3指数）。マクロ/政策/地政学/市場全体/半導体の話題だけに適用する。
    #    無関係な個別ニュースに「日経先物が動いた」点を配ると、全部が上位に来てしまうため。
    market_wide = bool(set(cats) & set(th.get("index_categories", [])))
    semis = any(k in title for k in cfg_imp.get("sector_market_keywords", []))
    idx_codes = list(th.get("index_codes", [])) if market_wide else (["^SOX", "NIY=F"] if semis else [])
    for code in idx_codes:
        s = _snap(facts, code)
        if not _usable(s, now, max_age):
            continue
        row = {"code": code, "name": s.get("name"), "chg_pct": round(s["chg_pct"], 2),
               "as_of": s.get("as_of")}
        used.append(row)
        if abs(s["chg_pct"]) >= th["index_pct"]:
            pts = max(pts, w["reaction_index"])
            if key_ind is None:
                key_ind = row
    if key_ind is None and used:
        key_ind = max(used, key=lambda r: abs(r["chg_pct"]))
    return pts, used, key_ind


def _keywords_hit(title: str, imp: dict) -> set[str]:
    words = set()
    for ws in (imp.get("keywords") or {}).values():
        words |= {w for w in ws if w in title}
    words |= {w for w in imp.get("semis_keywords", []) if w in title}
    return words


def _same_story(a: dict, b: dict, imp: dict) -> bool:
    """同じ話か。日本語の見出しは媒体ごとに言い回しが大きく違い、文字の類似度だけでは
    同じ評決の記事でも 0.1〜0.2 程度にしかならない（実測: キオクシア評決の各社見出し）。
    そこで次のいずれかで同一とみなす:
      1) 見出しの文字バイグラム類似度 >= similarity_threshold
      2) 同じ監視銘柄を含み、かつイベント種類のキーワードが1語以上共通
      3) 監視銘柄を含まない話題同士で、キーワードが2語以上共通し、類似度 >= similarity_floor
    """
    ca, cb = a["_cat"] - {"market_wide"}, b["_cat"] - {"market_wide"}
    # 個別企業の出来事（監視銘柄 + 訴訟/修正/M&A 等）は市況まとめに吸い込ませない。
    # 実測 2026-07-17: キオクシア評決の記事が「日経平均」を含むだけで市況側に入り、代表が市況記事になった。
    company_a = bool(a["_ent"] and ca & COMPANY_EVENTS)
    company_b = bool(b["_ent"] and cb & COMPANY_EVENTS)
    if a["_topic"] & b["_topic"] and not company_a and not company_b:
        return True  # 0) 同じ市況（米国市況/日本市況）
    sim = similarity(a["title"], b["title"])
    if sim >= imp.get("similarity_threshold", 0.35):
        return True
    if a["_ent"] & b["_ent"]:
        # 同じ監視銘柄:
        #  - 両方に種類があれば種類が共通のときだけ（賠償/評決 はどちらも scandal）
        #  - 片方だけ種類がある → 同じ銘柄の値動き記事などとして同じ話にまとめる
        #  - 両方とも種類なし → 文字の類似度の下限も課す（同じ銘柄の別の話を数珠つなぎに
        #    しないため。実測 2026-11-10: エヌビディアの名前だけで新製品・不祥事・市況が繋がった）
        if ca and cb:
            return bool(ca & cb)
        # 片方/両方に種類が無いときは、両方が「その銘柄の値動きの記事」のときだけまとめる。
        # 銘柄名が同じだけで繋ぐと無関係な話を吸い込む（実測 2026-11-06 再現: 任天堂の減配記事に
        # Nintendo Store のメンテ告知、キオクシアの急落記事に岩手工場の合併記事が入った）。
        # 文字の類似度も銘柄名の共有だけで 0.12 を超えるので、ここでは判定に使わない。
        mv = imp.get("move_words", [])
        return any(m in a["title"] for m in mv) and any(m in b["title"] for m in mv)
    if not a["_ent"] and not b["_ent"]:
        return len(a["_kw"] & b["_kw"]) >= 2 and sim >= imp.get("similarity_floor", 0.12)
    return False


def cluster(items: list[dict], imp: dict, focus: dict) -> list[list[dict]]:
    for it in items:
        it["_ent"] = {c for c, names in focus.items() if any(n and n in it["title"] for n in names)}
        it["_kw"] = _keywords_hit(it["title"], imp)
        it["_cat"] = set(classify(it["title"], imp))
        it["_topic"] = {t for t, ws in (imp.get("topics") or {}).items() if any(w in it["title"] for w in ws)}
    # 種(seed)方式: 各記事はグループの「種」とだけ比べる（ほぼ同文の重複だけは全メンバーと比べる）。
    # 全メンバーと比べると A~B, B~C の数珠つなぎで無関係な話が1つにまとまる
    # （実測 2026-11-06 再現: キオクシアの急落・岩手工場の合併・東エレ決算が1グループになった）。
    # 種になりやすい順 = 種類の付いた記事 → 市況記事 → その他、各々で上位の層・早い順。
    def seed_rank(x):
        strong = bool(x["_cat"] - {"market_wide"})
        return (0 if strong else 1 if x["_topic"] else 2, LAYER_RANK.get(x["layer"], 9),
                x["when"] or dt.datetime.max.replace(tzinfo=UTC))
    dup = imp.get("duplicate_threshold", 0.6)
    groups: list[list[dict]] = []
    for it in sorted(items, key=seed_rank):
        for g in groups:
            if _same_story(it, g[0], imp) or any(similarity(it["title"], x["title"]) >= dup for x in g):
                g.append(it)
                break
        else:
            groups.append([it])
    return groups


def score_cluster(g: list[dict], facts: dict, cfg: dict, focus: dict, now: dt.datetime) -> dict:
    imp = cfg["impact"]
    w = imp["weights"]
    # 代表 = 最上位の層。同じ層の中では、話題・種類キーワードを多く含む（=話の中心に近い）もの、
    # 次に早いもの。
    g = sorted(g, key=lambda x: (LAYER_RANK.get(x["layer"], 9),
                                 -(len(x.get("_kw", ())) + len(x.get("_topic", ()))),
                                 x["when"] or dt.datetime.max.replace(tzinfo=UTC)))
    rep = g[0]
    text = " ".join(x["title"] for x in g)
    cats = {c for x in g for c in classify(x["title"], imp)}
    if any(x.get("scheduled_earnings") for x in g):
        cats.add("scheduled_earnings")
    cats = sorted(cats)
    related = [code for code, names in focus.items() if any(n and n in text for n in names)]
    if rep.get("code"):
        c = rep["code"] + ".T"
        if c in focus and c not in related:
            related.append(c)

    breakdown = []
    react_pts, used, key_ind = market_reaction(text, cats, related, facts, imp, now)
    if react_pts:
        breakdown.append(("市場の反応", react_pts))
    type_pts = max([w.get(c, 0) for c in cats] or [0])  # 1つの出来事に種類は1つ。合算しない
    if type_pts:
        breakdown.append(("イベント種類:" + "/".join(c for c in cats if w.get(c, 0) == type_pts), type_pts))
    semis = any(k in text for k in imp.get("semis_keywords", []))
    if related or semis:
        breakdown.append(("保有・半導体に直結", w["focus"]))

    # 独立したソースの数。同じ見出しが複数のポータルに転載されたもの（Infoseek/au/地方局サイト等）は
    # 1本の記事なので1ソースと数える。実測 2026-11-09: 同一記事が4媒体名で並んでいた。
    distinct = {}
    for x in g:
        distinct.setdefault(_norm(x["title"]), x)
    indep = list(distinct.values())
    outlet = lambda x: x.get("outlet") or outlet_of(x["media"])  # noqa: E731
    n_sources = len({outlet(x) for x in indep})
    layers = {x["layer"] for x in indep}
    b_medias = {outlet(x) for x in indep if x["layer"] == "B"}
    confirmed = ("A" in layers and "B" in layers) or len(b_medias) >= 2
    if confirmed:
        breakdown.append(("異なる層/媒体で確認", w["cross_confirm"]))
    total = min(100, sum(p for _, p in breakdown))
    capped = False
    if n_sources <= 1 and total > imp["single_source_cap"]:
        total, capped = imp["single_source_cap"], True

    event = next((x.get("event") for x in g if x.get("event")), None)
    kind = "反応済み" if react_pts else "これから効く"
    return {"rep": rep, "members": g, "cats": cats, "related": related, "score": total,
            "breakdown": breakdown, "single_source_capped": capped, "indicators": used,
            "key_indicator": key_ind, "kind": kind, "event": event}


def upcoming_items(facts: dict, earnings: dict | None, cfg: dict, now: dt.datetime) -> list[dict]:
    """「これから効く」の予定を A層の記事と同じ形にする。日付と名称は公式/既存取得のまま。"""
    imp = cfg["impact"]
    days = imp.get("upcoming_days", 5)
    end = now.date() + dt.timedelta(days=days)
    out = []
    ec = facts.get("econ_calendar") or {}
    for x in (ec.get("today") or []) + (ec.get("upcoming") or []):
        try:
            d = dt.date.fromisoformat(x["date"])
        except (KeyError, ValueError):
            continue
        if not (now.date() <= d <= end) or x.get("importance") != "high":
            continue
        out.append({"title": f"{x['name']}（{x['date']}{' ' + x['time_jst'] if x.get('time_jst') else ''} JST予定）",
                    "link": x.get("source_url", ""), "media": "公式日程(config.yaml)", "layer": "A",
                    "when": None, "source_id": "econ_calendar",
                    "event": {"date": x["date"], "time_jst": x.get("time_jst"), "name": x["name"]}})
    focus_codes = {h["code"] for h in (cfg.get("holdings") or []) + (cfg.get("watch") or [])}
    focus_codes |= set((cfg.get("earnings") or {}).get("sector_bellwethers") or [])
    for e in (earnings or {}).get("events") or []:
        try:
            d = dt.date.fromisoformat(e["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if not (now.date() <= d <= end) or e.get("code") not in focus_codes or not e.get("confirmed"):
            continue
        out.append({"title": f"{e['name']} 決算発表（{e['date']}予定）", "link": "",
                    "media": f"決算日程({e.get('source')})", "layer": "A", "when": None,
                    "source_id": "earnings", "scheduled_earnings": True,
                    "event": {"date": e["date"], "time_jst": None, "name": f"{e['name']} 決算発表"}})
    return out


# ============================================================================
# 予測（LLM）。OpenAI互換エンドポイント。未設定・失敗時は予測欄なし。
# ============================================================================
_NUM = re.compile(r"[-+−▲▼]?\d[\d,]*(?:\.\d+)?")


def _nums(text: str) -> list[float]:
    out = []
    for m in _NUM.findall(str(text)):
        s = m.replace(",", "").replace("−", "-").replace("▲", "").replace("▼", "-").replace("+", "")
        try:
            out.append(float(s))
        except ValueError:
            pass
    return out


def _num_ok(v: float, allowed: list[float]) -> bool:
    # 丸め表記（-5.96 と -5.9647）は許す。符号違い（下落を上昇と書く）は許さない。
    return any(abs(v - a) <= max(0.006, abs(a) * 0.0005) for a in allowed)


def llm_config() -> dict | None:
    base = os.getenv("IMPACT_LLM_BASE_URL")
    model = os.getenv("IMPACT_LLM_MODEL")
    if not base or not model:
        return None
    return {"base_url": base.rstrip("/"), "model": model, "api_key": os.getenv("IMPACT_LLM_API_KEY", "")}


def _http_llm(conf: dict, messages: list, temperature: float, timeout: float) -> str:
    h = {"Content-Type": "application/json"}
    if conf.get("api_key"):
        h["Authorization"] = f"Bearer {conf['api_key']}"
    r = requests.post(f"{conf['base_url']}/chat/completions", headers=h, timeout=timeout,
                      json={"model": conf["model"], "messages": messages,
                            "temperature": temperature, "response_format": {"type": "json_object"}})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def validate_prediction(raw: str, verified: dict) -> tuple[dict | None, str | None]:
    """LLM応答を検査する。不合格なら (None, 理由)。数値は入力に存在するものだけ許す。"""
    try:
        p = json.loads(raw)
    except Exception:
        return None, "JSONとして解釈できない"
    if not isinstance(p, dict) or set(p) != {"direction", "reason", "risk", "numbers_used"}:
        return None, "スキーマ不一致"
    if p["direction"] not in ("上昇要因", "下落要因", "中立"):
        return None, f"direction 不正: {p['direction']!r}"
    if not isinstance(p["numbers_used"], list) or not all(isinstance(x, str) for x in (p["reason"], p["risk"])):
        return None, "型不一致"
    allowed = _nums(json.dumps(verified, ensure_ascii=False))
    used = [v for x in p["numbers_used"] for v in _nums(x)]
    if len(used) < len(p["numbers_used"]):
        return None, "numbers_used に数値でない要素"
    for v in used + _nums(p["reason"]) + _nums(p["risk"]):
        if not _num_ok(v, allowed):
            return None, f"入力に無い数値 {v} を使用"
    for t in (p["reason"], p["risk"]):
        if contains_banned(t):
            return None, f"禁止表現 {contains_banned(t)}"
        if len(t) > 80:
            return None, "文が長すぎる（1行80字以内）"
    return {"direction": p["direction"], "reason": p["reason"].strip(), "risk": p["risk"].strip(),
            "numbers_used": p["numbers_used"]}, None


SYSTEM_PROMPT = (
    "あなたは株式市場のニュースの影響を整理する補助役。入力JSONの事実と数値だけを使う。"
    "入力に無い数値は一切書かない。売買の推奨や「買い」「売り」「推奨」「狙い目」という語は使わない。"
    "方向は 上昇要因 / 下落要因 / 中立 のいずれか。出力は次のJSONだけ: "
    '{"direction": "上昇要因|下落要因|中立", "reason": "根拠1行(80字以内)", '
    '"risk": "外れるパターン1行(80字以内)", "numbers_used": ["使った数値を入力の表記のまま"]}'
)


def predict(verified: dict, conf: dict | None, temperature: float, timeout: float,
            caller=None) -> tuple[dict | None, str]:
    if not conf and caller is None:
        return None, "LLM未設定（無LLMモード）"
    try:
        raw = (caller or _http_llm)(conf, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(verified, ensure_ascii=False)}],
            min(temperature, 0.2), timeout)
    except Exception as e:
        return None, f"LLM失敗: {type(e).__name__}"
    pred, why = validate_prediction(raw, verified)
    return (pred, "ok") if pred else (None, f"予測を破棄: {why}")


# ============================================================================
# 組み立て
# ============================================================================
def _fmt_when(w: dt.datetime | None) -> str:
    return w.astimezone(JST).strftime("%m/%d %H:%M") if w else "日時不明"


def _headline(title: str, n: int = 22) -> str:
    return title if len(title) <= n else title[:n] + "…"


def _level(score: int) -> str:
    return "🔴" if score >= 80 else "🟠" if score >= 70 else "🟡"


def build_item(c: dict, focus: dict, facts: dict) -> dict:
    rep = c["rep"]
    title, adj = sanitize(rep["title"])
    tag = "（※表記調整）" if adj else ""
    if c["event"]:
        fact = f"{rep['media']}によると {c['event']['name']} が {c['event']['date']}" \
               f"{' ' + c['event']['time_jst'] + ' JST' if c['event'].get('time_jst') else ''} に予定。"
    else:
        fact = f"{rep['media']}（{_fmt_when(rep['when'])}）:「{title}」{tag}"
    fact = fact if len(fact) <= 80 else fact[:79] + "…"
    names = []
    for code in c["related"]:
        s = _snap(facts, code) or {}
        nm = (focus.get(code) or [s.get("name") or code])[0]
        held = "保有" if code in {k for k in (facts.get("holdings") or {})} else ""
        names.append(f"{nm}({code}{'・' + held if held else ''})")
    seen, sources = set(), []
    for m in c["members"]:
        if m["link"] in seen and m["link"]:
            continue
        seen.add(m["link"])
        sources.append({"media": m["media"], "layer": m["layer"], "url": m["link"],
                        "title": sanitize(m["title"])[0], "when": _fmt_when(m["when"])})
    return {
        "headline": sanitize(_headline(rep["title"]))[0],
        "score": c["score"], "level": _level(c["score"]), "kind": c["kind"],
        "categories": c["cats"],
        "score_breakdown": [{"rule": r, "points": p} for r, p in c["breakdown"]],
        "single_source_capped": c["single_source_capped"],
        "fact": fact,
        "market_reaction": c["indicators"],
        "key_indicator": c["key_indicator"],
        "affected": names,
        "affects_holdings": any(code in (facts.get("holdings") or {}) for code in c["related"]),
        "prediction": None, "prediction_status": None,
        "sources": sources,
    }


CATEGORY_LABELS = {
    "politics_geo": "政治・地政学", "monetary_macro": "金融政策・指標", "earnings_revision": "決算修正",
    "scandal": "不祥事・訴訟", "ma_buyback": "M&A・自社株", "scheduled_earnings": "決算予定",
    "market_wide": "市況",
}


def board_row(c: dict, focus: dict, facts: dict, imp: dict) -> dict:
    """一覧用の1行。見出し・点数・部門・媒体名・代表URLだけ（本文は持たない）。"""
    rep = c["rep"]
    title, adj = sanitize(rep["title"])
    text = " ".join(x["title"] for x in c["members"])
    tags = [CATEGORY_LABELS[k] for k in c["cats"] if k in CATEGORY_LABELS]
    if c["related"] or any(k in text for k in imp.get("semis_keywords", [])):
        tags.append("保有・半導体")
    outlets = list(dict.fromkeys(x["media"] for x in c["members"]))
    k = c["key_indicator"]
    return {
        "headline": title if len(title) <= 60 else title[:59] + "…",
        "adjusted": adj,  # 禁止表現の言い換えをしたか（原文そのままではない印）
        "score": c["score"], "kind": c["kind"], "tags": tags or ["その他"],
        "media": outlets[:4], "n_media": len(outlets), "url": rep["link"],
        "when": _fmt_when(rep["when"]),
        "capped": c["single_source_capped"],
        "breakdown": [{"rule": sanitize(r)[0], "points": p} for r, p in c["breakdown"]],
        "indicator": ({"name": k.get("name"), "chg_pct": k.get("chg_pct"), "as_of": k.get("as_of")}
                      if k else None),
    }


def dashboard_board(imp: dict | None) -> list:
    return list((imp or {}).get("board") or [])


def build(facts: dict, cfg: dict, earnings: dict | None = None, now: dt.datetime | None = None,
          fetcher=None, sources: list[dict] | None = None, llm_caller=None,
          use_llm: bool = True, after: dt.date | None = None, before: dt.date | None = None) -> dict:
    t0 = time.time()
    imp = cfg["impact"]
    now = now or _now()
    start = window_start(now)
    focus = focus_names(cfg)

    items, results = reuse_existing(facts)
    got, res2 = fetch_sources(sources if sources is not None else SOURCES,
                              imp.get("request_timeout_sec", 10), fetcher, after, before)
    items += got
    results += res2
    ok_sources = [r for r in results if not r["error"]]
    errors = [r for r in results if r["error"]]

    blocked = imp.get("blocked_publishers") or []
    pool, seen = [], set()
    for it in items:
        if any(b in (it["media"] or "") for b in blocked):
            continue
        # 同じ媒体の同じ見出しは1件に（既存⑥-Bと追加ソースの両方で拾った場合など）
        key = (_norm(it["title"]), it["media"])
        if key in seen:
            continue
        seen.add(key)
        # 対象期間: 前営業日15:30〜実行時刻。日時が無い記事は期間を確認できないので使わない
        if it["when"] is None or not (start <= it["when"] <= now + dt.timedelta(minutes=5)):
            continue
        pool.append(it)
    pool += upcoming_items(facts, earnings, cfg, now)

    clusters = [score_cluster(g, facts, cfg, focus, now) for g in cluster(pool, imp, focus)]
    clusters.sort(key=lambda c: (-c["score"], LAYER_RANK.get(c["rep"]["layer"], 9)))
    picked = [c for c in clusters if c["score"] >= imp["min_score"]][:imp.get("max_items", 5)]

    out_items = [build_item(c, focus, facts) for c in picked]
    # 一覧用: 60点未満も含めた全候補（点数順）。上位5件の選定とは独立で、Discord/.md には出さない。
    board = [board_row(c, focus, facts, imp) for c in clusters
             if c["score"] >= imp.get("board_min_score", 20)][:imp.get("board_max_rows", 80)]

    conf = llm_config() if use_llm else None
    for it in out_items:
        if time.time() - t0 > imp.get("llm_budget_sec", 70):
            it["prediction_status"] = "時間切れのため予測欄なし"
            continue
        verified = {"headline": it["headline"], "fact": it["fact"], "kind": it["kind"],
                    "categories": it["categories"], "market_reaction": it["market_reaction"],
                    "affected": it["affected"]}
        pred, st = predict(verified, conf, imp.get("llm_temperature", 0.2),
                           imp.get("request_timeout_sec", 10),
                           caller=llm_caller if use_llm else None)
        it["prediction"], it["prediction_status"] = pred, st

    if not ok_sources:
        status = "failed"
    elif out_items:
        status = "ok"
    else:
        status = "empty"
    return {
        "status": status,
        "status_text": {"ok": f"{len(out_items)}件", "empty": "本日は大きな材料なし（該当なし）",
                        "failed": "取得できていません（全ソース取得失敗）"}[status],
        "window_jst": [start.isoformat(), now.isoformat()],
        "items": out_items,
        "board": board,
        "n_candidates": len(pool), "n_clusters": len(clusters),
        "near_miss": [{"headline": sanitize(_headline(c["rep"]["title"]))[0], "score": c["score"]}
                      for c in clusters if c["score"] < imp["min_score"]][:5],
        "sources": results,
        "source_errors": [{"id": r["id"], "error": r["error"]} for r in errors],
        "llm": "無効" if not use_llm else ("未設定" if not conf and llm_caller is None else "有効"),
        "elapsed_sec": round(time.time() - t0, 2),
        "note": "点数はルール計算（config.yaml impact:）。市場の反応は同時間帯の指標の前日比であり、"
                "因果関係を検証したものではない。",
    }


# ============================================================================
# 出力（.md / Discord / dashboard）
# ============================================================================
def render_md(imp: dict | None) -> list[str]:
    L = ["\n\n## ⑪ 株価インパクトニュース\n"]
    if not imp:
        L.append("**取得できていません** — インパクト判定が生成されていません。")
        return L
    if imp["status"] == "failed":
        L.append("**取得できていません** — 全ソースの取得に失敗（材料ゼロではない）。")
        for e in imp["source_errors"]:
            L.append(f"- {e['id']}: {e['error']}")
        return L
    if imp["status"] == "empty":
        L.append("本日は大きな材料なし（60点以上の該当なし）。")
    for i, it in enumerate(imp["items"], 1):
        L.append(f"\n### {i}. {it['level']} {it['headline']}（{it['score']}点・{it['kind']}）")
        L.append(f"- 【事実】{it['fact']}")
        mr = it["market_reaction"]
        L.append("- 【市場の反応】" + (" / ".join(
            f"{r['name']} {r['chg_pct']:+.2f}%（{r['as_of']}）" for r in mr) if mr else "対応する指標なし"))
        L.append("- 【効く業種・銘柄】" + ("・".join(it["affected"]) if it["affected"] else "特定なし（市場全体）")
                 + ("（保有銘柄に関係）" if it["affects_holdings"] else ""))
        p = it.get("prediction")
        if p:
            L.append(f"- 【予測】{p['direction']} — {p['reason']} / 外れるパターン: {p['risk']}。"
                     f"{PREDICTION_NOTE}")
        else:
            L.append(f"- 【予測】なし（{it.get('prediction_status') or '無LLMモード'}）")
        L.append("- 【出典】" + " / ".join(f"[{s['media']}]({s['url']})" if s["url"] else s["media"]
                                          for s in it["sources"]))
        L.append(f"- 点数内訳: " + " + ".join(f"{b['rule']} {b['points']}" for b in it["score_breakdown"])
                 + ("（1ソースのみのため上限50）" if it["single_source_capped"] else ""))
    if imp["source_errors"]:
        L.append(f"\n_一部ソース取得失敗: " + ", ".join(f"{e['id']}({e['error']})" for e in imp["source_errors"]) + "_")
    L.append(f"\n_{imp['note']} 対象期間 {imp['window_jst'][0][:16]}〜{imp['window_jst'][1][:16]} JST_")
    return L


def discord_fields(imp: dict | None, budget: int = 2600) -> list[dict]:
    """Discord embed のフィールド。全体6000字の上限があるため budget を超えたら
    出典URLを省き、省いたと明記する（黙って落とさない）。"""
    name = "⑪ 株価インパクトニュース"
    if not imp:
        return [{"name": name, "value": "⚠️ 取得できていません（判定が生成されていません）", "inline": False}]
    if imp["status"] == "failed":
        return [{"name": name, "value": "⚠️ 取得できていません — 全ソース取得失敗。材料ゼロではない。",
                 "inline": False}]
    if imp["status"] == "empty":
        return [{"name": name, "value": "本日は大きな材料なし（該当なし）", "inline": False}]

    def _field(it: dict, with_urls: bool) -> dict:
        mr = " / ".join(f"{r['name']} {r['chg_pct']:+.2f}%" for r in it["market_reaction"][:3]) or "対応指標なし"
        p = it.get("prediction")
        pred = (f"{p['direction']} — {p['reason']}／外れる場合: {p['risk']}。{PREDICTION_NOTE}"
                if p else "なし（無LLMモード）")
        if with_urls:
            src = " ".join(f"[{s['media']}]({s['url']})" if s["url"] else s["media"] for s in it["sources"])
        else:
            src = "・".join(dict.fromkeys(s["media"] for s in it["sources"])) + "（URLは添付.md）"
        v = (f"【事実】{it['fact']}\n【市場の反応】{mr}\n"
             f"【効く業種・銘柄】{'・'.join(it['affected']) or '市場全体'}\n【予測】{pred}\n【出典】{src}")
        if len(v) > 1024 and with_urls:
            # 途中で切るとリンクが壊れる。URLを省いた版に切り替え、省いたと明記する
            return _field(it, False)
        if len(v) > 1024:
            v = v[:1000] + "…（続きは添付.md）"
        return {"name": f"{it['level']} {it['headline']}（{it['score']}点・{it['kind']}）"[:256],
                "value": v, "inline": False}

    fields = [_field(it, True) for it in imp["items"]]
    if sum(len(f["name"]) + len(f["value"]) for f in fields) > budget:
        fields = [_field(it, False) for it in imp["items"]]
    return fields


def dashboard_block(imp: dict | None) -> tuple[list, str]:
    if not imp:
        return [], "failed"
    rows = []
    for it in imp["items"]:
        rows.append({k: it[k] for k in ("headline", "score", "level", "kind", "fact", "market_reaction",
                                       "affected", "affects_holdings", "prediction", "sources")})
        if rows[-1]["prediction"]:
            rows[-1]["prediction"] = {**rows[-1]["prediction"], "note": PREDICTION_NOTE}
    return rows, imp["status"]


# ============================================================================
# 記録（data/impact_log.csv）
# ============================================================================
LOG_COLS = ["date", "generated_at_jst", "headline", "score", "kind", "direction",
            "indicator", "indicator_name", "indicator_chg_pct_at_report", "indicator_as_of",
            "next_chg_pct", "next_as_of", "next_filled_at_jst"]


def log_path() -> pathlib.Path:
    return ROOT / "data" / "impact_log.csv"


def append_log(imp: dict, now: dt.datetime, path: pathlib.Path | None = None) -> int:
    """1日1回分だけ記録する。同じ日・同じ見出しの行が既にあれば追記しない
    （手動の --force 実行で同じ材料が何行も入ると、後の集計が水増しされる）。"""
    path = path or log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    seen = set()
    if not new:
        with path.open(encoding="utf-8-sig", newline="") as f:
            seen = {(r["date"], r["headline"]) for r in csv.DictReader(f)}
    n = 0
    with path.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLS)
        if new:
            w.writeheader()
        for it in imp.get("items") or []:
            if (now.date().isoformat(), it["headline"]) in seen:
                continue
            k = it.get("key_indicator") or {}
            w.writerow({"date": now.date().isoformat(), "generated_at_jst": now.isoformat()[:19],
                        "headline": it["headline"], "score": it["score"], "kind": it["kind"],
                        "direction": (it.get("prediction") or {}).get("direction", ""),
                        "indicator": k.get("code", ""), "indicator_name": k.get("name", ""),
                        "indicator_chg_pct_at_report": k.get("chg_pct", ""),
                        "indicator_as_of": k.get("as_of", ""),
                        "next_chg_pct": "", "next_as_of": "", "next_filled_at_jst": ""})
            n += 1
    return n


def fill_next_day(hist: dict, now: dt.datetime, path: pathlib.Path | None = None) -> int:
    """未記入の行に、記録時の指標日付の「次の足」の前日比を実測で埋める。
    次の足がまだ無い・指標の履歴が無い行は空欄のまま残す（推定で埋めない）。"""
    path = path or log_path()
    if not path.exists():
        return 0
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    filled = 0
    for r in rows:
        if r.get("next_chg_pct") or not r.get("indicator") or not r.get("indicator_as_of"):
            continue
        df = hist.get(r["indicator"])
        if df is None or getattr(df, "empty", True):
            continue
        try:
            base = dt.date.fromisoformat(r["indicator_as_of"][:10])
        except ValueError:
            continue
        dates = [d.date() if hasattr(d, "date") else d for d in df.index]
        idx = [i for i, d in enumerate(dates) if d > base]
        if not idx or idx[0] == 0:
            continue
        i = idx[0]
        prev, cur = float(df["Close"].iloc[i - 1]), float(df["Close"].iloc[i])
        if not prev:
            continue
        r["next_chg_pct"] = f"{(cur / prev - 1) * 100:.2f}"
        r["next_as_of"] = dates[i].isoformat()
        r["next_filled_at_jst"] = now.isoformat()[:19]
        filled += 1
    if filled:
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LOG_COLS)
            w.writeheader()
            w.writerows(rows)
    return filled


# ============================================================================
# 実測プローブ（docs/impact_sources.md の元データ）
# ============================================================================
def probe() -> None:
    now = dt.datetime.now(UTC)
    print("| 層 | ソース | HTTP | 件数 | 最新記事(JST) | 経過 | 判定 |")
    print("|---|---|---:|---:|---|---:|---|")
    for s in SOURCES:
        st, body, err = fetch_raw(source_url(s), 10)
        n, latest = 0, None
        if body is not None and not err:
            for it in parse_feed(s, body):
                n += 1
                if it["when"] and (latest is None or it["when"] > latest):
                    latest = it["when"]
        age = (now - latest).total_seconds() / 3600 if latest else None
        ok = (not err) and n > 0 and age is not None and age < 48
        print(f"| {s['layer']} | {s['media']} | {st} | {n} | "
              f"{latest.astimezone(JST):%Y-%m-%d %H:%M}" if latest else f"| {s['layer']} | {s['media']} | {st} | {n} | -",
              end="")
        print(f" | {age:.1f}h | {'採用' if ok else '不採用'} |" if age is not None else f" | - | {'採用' if ok else '不採用'} |")


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
