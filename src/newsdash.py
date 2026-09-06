"""
ニュースダッシュボード(news_dashboard.html)が読む out/newsdash.json を作る。

このファイルは今まで生成元がリポジトリに無く、手作業で作られたまま
2026-09-05 で止まっていた。平日3回の定時配信を載せるので、再現可能な生成元を置く。

出力の契約（news_dashboard.html がこのキーを読む。勝手に変えないこと）:
  generated_at : ISO8601 (JST)
  markets      : [{name, code, close, chg_pct, as_of, age_bdays, stale, stale_warning}]
  jgb          : {base_date, age_days, curve:{年限:利回り}, stale} / 取れなければ {status}
  headlines    : {status, items:[...], by_category:{カテゴリ:[...]}}
  failed       : ["取得できなかったソース名: 理由", ...]

このプロジェクトの原則どおり、取れなかったものは failed に積んで明示する。
「記事ゼロ」と「取得失敗」を同じ空配列にしない。

【重要度の付け方について】
元の newsdash.json にも tier/score/hits があったが、その生成コードは残っていない。
ここに書くのは**再実装**であり、過去の値と一致することは保証しない。
数え方は下の SCORE_WORDS が全てで、推測や外部モデルは一切入らない。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import feedparser
import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import collect  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
ROOT = pathlib.Path(__file__).resolve().parents[1]
_UA = {"User-Agent": "Mozilla/5.0 (morning-report/1.0)"}

# ---------------------------------------------------------------------------
# RSS ソース
# 2026-09-06 に本人PCで到達性を実測し、200 が返って entries が取れたものだけ。
# NHK(nhk/news,economy,international,main,business,world) / 東洋経済(all,latest,news) /
# ダイヤモンド(diamond/all) / 日経クロステック(xtech/all) はいずれも 403 だった。
# 過去の newsdash.json にはこれらの記事が入っているので、以前は取れていたはず。
# 復活したら SOURCES に足す。取れない間は failed に出して欠落を隠さない。
# ---------------------------------------------------------------------------
SOURCES = [
    ("日経 速報",     "nikkei/news",      "主要・速報"),
    ("日経 政治・経済", "nikkei/economy",   "マーケット・経済"),
    ("日経 ビジネス",  "nikkei/business",  "企業・ビジネス"),
    ("産経 経済",     "sankei/economy",   "マーケット・経済"),
    ("産経 国際",     "sankei/world",     "国際・政治"),
    ("産経 政治",     "sankei/politics",  "国際・政治"),
    ("読売 経済",     "yomiuri/economy",  "マーケット・経済"),
    ("読売 国際",     "yomiuri/world",    "国際・政治"),
    ("読売 政治",     "yomiuri/politics", "国際・政治"),
]
# 以前は取れていたが現在403のソース。失敗として毎回明示する（黙って減らさない）。
KNOWN_DOWN = [
    ("NHK",         "nhk/economy"),
    ("東洋経済",     "toyokeizai/all"),
    ("ダイヤモンド", "diamond/all"),
    ("日経クロステック", "xtech/all"),
]
FEED = "https://assets.wor.jp/rss/rdf/{p}.rdf"

# 株に関係する記事だけを残すためのキーワードと重み。
# 1語も当たらない記事は落とす（この画面は市場を見るためのもので、総合ニュースではない）。
SCORE_WORDS = {
    # 相場そのもの
    "株価": 4, "相場": 3, "株式": 3, "日経平均": 5, "TOPIX": 4, "東証": 3,
    "為替": 3, "円安": 4, "円高": 4, "長期金利": 4, "国債": 3,
    # 金融政策
    "日銀": 5, "FRB": 5, "利上げ": 4, "利下げ": 4, "金利": 3, "金融政策": 4,
    "雇用統計": 4, "CPI": 3, "物価": 2, "インフレ": 3,
    # 企業活動
    "決算": 4, "上方修正": 5, "下方修正": 5, "業績": 3, "増配": 3, "減配": 3,
    "自社株買い": 4, "TOB": 4, "買収": 3, "上場": 2, "投資": 2, "企業": 1,
    # セクター
    "半導体": 4, "AI": 2, "データセンター": 3, "電線": 3, "原油": 2, "銅": 2,
    # マクロ・政治
    "関税": 3, "経済": 1, "政府": 1, "首相": 1, "大統領": 1, "銀行": 1,
    "産業": 1, "市場": 1, "税": 1, "貿易": 2, "輸出": 2, "景気": 3,
}
# 2026-09-06(日)夕方の実データ34件で score の分布は最大7・中央1だった。
# 閾値8では「高」が1件も出ないので 6/3 にする。平日の決算・日銀ネタが入る時間帯は
# もっと上振れるはずなので、運用してみて偏るようならここを動かす。
TIER_HIGH, TIER_MID = 6, 3

# キーワード一致なので誤検出は残る（例:「旧日銀支店の活用議論」が『日銀』で拾われる）。
# 見出しの意味を機械で判定はしない。重要度はあくまで語の出現の目安として出す。

MARKETS = [
    ("^N225", "日経平均"), ("998405.T", "TOPIX"), ("JPY=X", "ドル円"),
    ("^TNX", "米10年金利"), ("^DJI", "NYダウ"), ("^IXIC", "NASDAQ"),
    ("^SOX", "SOX指数"), ("CL=F", "WTI原油"), ("^VIX", "VIX"),
]

MOF_CSV = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
JGB_TENORS = ("2年", "5年", "10年", "20年", "30年", "40年")


# ---------------------------------------------------------------------------
# 市況タイル
# ---------------------------------------------------------------------------
def fetch_markets() -> tuple[list[dict], list[str]]:
    """9指標の終値と前日比。collect.snapshot をそのまま使い、鮮度判定も引き継ぐ。"""
    rows, failed = [], []
    for code, name in MARKETS:
        try:
            if code == "998405.T":
                # TOPIXは yfinance に配信が無い。株のダッシュボードと同じ別ソースを使う。
                import yahoo_jp
                s = yahoo_jp.fetch_topix()
                if s.get("status"):
                    failed.append(f"{name}: {s['status']}")
                    continue
                rows.append({"name": name, "code": code, "close": s.get("close"),
                             "chg_pct": s.get("chg_pct"), "as_of": s.get("as_of"),
                             "age_bdays": None, "stale": False, "stale_warning": None})
                continue
            df = collect.fetch_history(code)
            s = collect.snapshot(code, name, df)
            if s.get("status"):
                failed.append(f"{name}: {s['status']}")
                continue
            rows.append({"name": name, "code": code, "close": s["close"],
                         "chg_pct": s["chg_pct"], "as_of": s["as_of"],
                         "age_bdays": s["data_age_bdays"], "stale": s["stale"],
                         "stale_warning": s["stale_warning"]})
        except Exception as e:
            failed.append(f"{name}: 取得失敗 {type(e).__name__}: {e}")
    return rows, failed


# ---------------------------------------------------------------------------
# 国債イールドカーブ（財務省)
# ---------------------------------------------------------------------------
def _wareki(s: str) -> dt.date | None:
    """'R8.9.3' -> date(2026,9,3)。令和以外の元号は来ない前提だが、来たらNoneを返す。"""
    s = s.strip()
    if not s.startswith("R"):
        return None
    try:
        y, m, d = s[1:].split(".")
        return dt.date(2018 + int(y), int(m), int(d))  # 令和1年 = 2019
    except (ValueError, TypeError):
        return None


def fetch_jgb() -> dict:
    """財務省の国債金利情報CSV。日付は和暦なので西暦に直す。

    2026-09-06 実測: 当月分だけが載る小さなCSV(7行)で、最終行は注意書き。
    値が '-' の年限があるので float 変換に失敗したら入れない（0で埋めない）。
    """
    try:
        r = requests.get(MOF_CSV, timeout=40, headers=_UA)
        r.raise_for_status()
        lines = r.content.decode("cp932", errors="replace").splitlines()
    except Exception as e:
        return {"status": f"取得失敗: {type(e).__name__}: {e}"}

    header, rows = None, []
    for ln in lines:
        cells = [c.strip() for c in ln.split(",")]
        if cells and cells[0] == "基準日":
            header = cells
        elif header and _wareki(cells[0]):
            rows.append(cells)
    if not header or not rows:
        return {"status": "CSVの形式が想定と違う（基準日の行が見つからない）"}

    last = rows[-1]
    base = _wareki(last[0])
    curve = {}
    for tenor in JGB_TENORS:
        if tenor not in header:
            continue
        try:
            curve[tenor] = float(last[header.index(tenor)])
        except (ValueError, IndexError):
            pass  # '-' や欠損。埋めない
    if not curve:
        return {"status": "利回りを1つも読めなかった"}

    age = (dt.datetime.now(JST).date() - base).days
    return {"base_date": base.isoformat(), "age_days": age, "curve": curve,
            # 営業日ベースではなく暦日。3日以上開いていたら連休か更新停止を疑う。
            "stale": age >= 5}


# ---------------------------------------------------------------------------
# 見出し
# ---------------------------------------------------------------------------
def _score(title: str) -> tuple[int, list[str]]:
    hits = [w for w in SCORE_WORDS if w in title]
    return sum(SCORE_WORDS[w] for w in hits), hits


def fetch_headlines(hours: int = 30) -> tuple[dict, list[str]]:
    """新聞社RSSを集めて、株に関係する見出しだけを重要度付きで返す。

    返す形は news_dashboard.html の renderNews() が読むものに合わせる（推測しない）:
      status, window_hours, feeds_ok, feeds_total, collected, total, dropped,
      items[], by_tier{高/中/低: [...]}, feeds[{label,status}]
    by_tier であって by_category ではない。画面は重要度で並べる。
    """
    items, failed, ok_sources = [], [], 0
    collected, dropped = 0, 0
    feeds: list[dict] = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)

    for name, path, category in SOURCES:
        try:
            f = feedparser.parse(FEED.format(p=path))
            st = getattr(f, "status", 200)
            if st >= 400 or not f.entries:
                failed.append(f"{name}: HTTP {st} / 記事0件")
                feeds.append({"label": name, "status": f"HTTP {st} / 記事0件"})
                continue
        except Exception as e:
            failed.append(f"{name}: {type(e).__name__}: {e}")
            feeds.append({"label": name, "status": f"{type(e).__name__}: {e}"})
            continue

        ok_sources += 1
        feeds.append({"label": name, "status": "ok"})
        for e in f.entries:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            t = e.get("published_parsed") or e.get("updated_parsed")
            when = dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else None
            if when and when < cutoff:
                continue
            collected += 1
            score, hits = _score(title)
            if not hits:
                dropped += 1   # 株に関係しない記事。件数は残して画面に出す
                continue
            items.append({
                "title": title,
                "link": e.get("link") or "",
                "source": name,
                "category": category,
                "published": when.astimezone(JST).strftime("%Y-%m-%dT%H:%M") if when else None,
                "tier": "高" if score >= TIER_HIGH else "中" if score >= TIER_MID else "低",
                "score": score,
                "hits": hits,
            })

    for name, path in KNOWN_DOWN:
        failed.append(f"{name}: 取得不可（{path} が403）")
        feeds.append({"label": name, "status": f"取得不可（{path} が403）"})

    # 同じ記事が複数紙に出ることがある。題名で重複を落とす（先勝ち）。
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: (-x["score"], x["published"] or "")):
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        uniq.append(it)

    # 画面は重要度で並べる。カテゴリは各見出しのチップとして出るだけ。
    by_tier: dict[str, list] = {}
    for it in uniq:
        by_tier.setdefault(it["tier"], []).append(it)
    by_cat: dict[str, list] = {}
    for it in uniq:
        by_cat.setdefault(it["category"], []).append(it)

    if ok_sources == 0:
        status = "全ソース取得失敗"
    elif not uniq:
        status = "該当する記事なし"
    else:
        status = "ok"
    return {
        "status": status,
        "window_hours": hours,
        "feeds_ok": ok_sources,
        "feeds_total": len(SOURCES) + len(KNOWN_DOWN),
        "collected": collected,
        "total": len(uniq),
        "dropped": dropped,
        "items": uniq,
        "by_tier": by_tier,
        # by_category は画面では使っていないが、Discord側(notify_news.py)が
        # カテゴリ別にフィールドを作るので残す。
        "by_category": by_cat,
        "feeds": feeds,
        "n_sources_ok": ok_sources, "n_sources": len(SOURCES),
    }, failed


# ---------------------------------------------------------------------------
def build() -> dict:
    markets, mf = fetch_markets()
    jgb = fetch_jgb()
    headlines, hf = fetch_headlines()
    failed = mf + hf
    if jgb.get("status"):
        failed.append(f"国債金利(財務省): {jgb['status']}")
    return {
        "generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "markets": markets,
        "jgb": jgb,
        "headlines": headlines,
        "failed": failed,
    }


def write(out_dir: pathlib.Path) -> dict:
    import json
    data = build()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "newsdash.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


if __name__ == "__main__":
    d = write(ROOT / "out")
    h = d["headlines"]
    print(f"generated_at={d['generated_at']}")
    print(f"markets={len(d['markets'])} / jgb={'ok' if 'curve' in d['jgb'] else d['jgb'].get('status')}")
    print(f"headlines: {h['status']} / {len(h['items'])}件 "
          f"（ソース {h['n_sources_ok']}/{h['n_sources']}）")
    for k, v in h["by_category"].items():
        print(f"   {k}: {len(v)}")
    for f in d["failed"]:
        print(f"   失敗: {f}")
