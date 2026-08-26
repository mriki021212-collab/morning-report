"""
Yahoo!ファイナンス(日本)から、yfinance では取得できない2種類の値を取る。

なぜ yfinance ではないのか（2026-08-26 に実測した結果）:
  - TOPIX 指数そのもの: ^TOPX / ^TPX / TOPX / ^TOPIX / 998405.T いずれも yfinance は
    404 "possibly delisted" を返す。TOPIX連動ETF(1306.T 等)は取れるが、それは ETF の
    価格であって指数値ではないので「TOPIX」として出すことはできない。
  - 投資信託の基準価額: yfinance に協会コードの概念が無く、そもそも扱えない。
  - Stooq は JavaScript の proof-of-work によるボット判定を挟むようになったため使わない。
  - JPX公式の topixyear_j.xls は「年次」データ（最終行が2025年）で日次には使えない。
  - J-Quants の /indices/topix は 403 を返す = エンドポイントは存在するが要認証。
    JQ_REFRESH_TOKEN はGitHub Secretsにしか無くローカルで実証できないため採用しない。

このモジュールは HTML スクレイピングであり、先方のページ構造が変われば壊れる。
壊れたときに「前回値」や「それらしい値」を返すことは絶対にしない。取れなければ
{"status": "取得失敗: ..."} を返し、上位が欠損として表示する。これはこのリポジトリで
過去に繰り返し起きた「例外を握り潰して前回値を返す」バグを再発させないための方針。

実測確認（2026-08-26）:
  TOPIX  https://finance.yahoo.co.jp/quote/998405.T/history
         → 日付/始値/高値/安値/終値のHTMLテーブル。2026/8/25 終値 4,093.67、
           前日 2026/8/24 4,073.29。同ページの priceBoard 側 previousPrice=4,093.67 /
           previousPriceDate=08/25 と一致することを突き合わせて確認済み。
  投信   https://finance.yahoo.co.jp/quote/03311187
         → __PRELOADED_STATE__ の mainFundPriceBoard.fundPrices。
           eMAXIS Slim米国株式(S&P500) 44,836円 は運用会社公式CSV
           https://www.am.mufg.jp/fund_file/setteirai/253266.csv の 2026/08/25 行と一致。
"""
from __future__ import annotations

import datetime as dt
import json
import re

import requests

JST = dt.timezone(dt.timedelta(hours=9))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) morning-report/1.0"
TIMEOUT = 25

TOPIX_CODE = "998405.T"
TOPIX_NAME = "TOPIX"


def _get(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _num(s: str) -> float | None:
    """'4,093.67' -> 4093.67。数値として読めなければ None（0 で埋めない）。"""
    if s is None:
        return None
    s = re.sub(r"[,\s円]", "", str(s))
    try:
        v = float(s)
    except ValueError:
        return None
    return v


def _resolve_mmdd(mmdd: str, now: dt.datetime) -> str | None:
    """'08/25' を YYYY-MM-DD にする。年は跨ぎを考慮し、未来日になるなら前年とみなす。"""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})\s*$", mmdd or "")
    if not m:
        return None
    mo, da = int(m.group(1)), int(m.group(2))
    for year in (now.year, now.year - 1):
        try:
            d = dt.date(year, mo, da)
        except ValueError:
            continue
        if d <= now.date():
            return d.isoformat()
    return None


# ---------------------------------------------------------------- TOPIX

def _parse_history_rows(html: str) -> list[tuple[str, float]]:
    """履歴テーブルから (YYYY-MM-DD, 終値) を新しい順に返す。"""
    rows: list[tuple[str, float]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if len(cells) < 5:
            continue
        m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", cells[0])
        if not m:
            continue  # ヘッダ行など
        close = _num(cells[4])
        if close is None:
            continue
        rows.append((f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}", close))
    return rows


def fetch_topix() -> dict:
    """TOPIX の直近確定終値と前日比。facts["macro"] に入れられる形で返す。

    返すのは日足の確定終値であり、場中値ではない。macro の他の指標(yfinance日足)と
    同じ「前営業日終値ベース」に揃えるため。
    """
    base = {"code": TOPIX_CODE, "name": TOPIX_NAME,
            "source": "Yahoo!ファイナンス(日本) 998405.T 日足履歴"}
    url = f"https://finance.yahoo.co.jp/quote/{TOPIX_CODE}/history"
    try:
        rows = _parse_history_rows(_get(url))
    except Exception as e:  # noqa: BLE001 — 失敗を値で隠さず status として上に返す
        return {**base, "status": f"取得失敗: {type(e).__name__}: {e}"}

    if len(rows) < 2:
        return {**base, "status": f"取得失敗: 日足行を{len(rows)}行しか抽出できなかった"}

    rows.sort(key=lambda r: r[0])          # 古い順
    (prev_d, prev_c), (last_d, last_c) = rows[-2], rows[-1]
    if not prev_c:
        return {**base, "status": "取得失敗: 前日終値が0のため前日比を算出できない"}

    return {
        **base,
        "as_of": last_d,
        "close": round(last_c, 2),
        "prev_close": round(prev_c, 2),
        "prev_close_date": prev_d,
        "chg_pct": round((last_c / prev_c - 1) * 100, 4),
        "fetched_at_jst": dt.datetime.now(JST).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- 投資信託

def _preloaded_state(html: str) -> dict:
    m = re.search(r"__PRELOADED_STATE__\s*=\s*(\{.*?\});?\s*</script>", html, re.S)
    if not m:
        raise ValueError("__PRELOADED_STATE__ が見つからない（ページ構造が変わった可能性）")
    return json.loads(m.group(1))


def fetch_fund(code: str, name: str) -> dict:
    """投資信託1本の基準価額。code は協会コード(8桁)。

    テクニカル(RSI/移動平均乖離)は算出しない。日次1本値しか無く、株式と同じ指標を
    当てても意味が違ってしまうため。ここが返すのは基準価額と前日比だけ。
    """
    base = {"code": code, "name": name,
            "source": f"Yahoo!ファイナンス(日本) 投信 {code}"}
    url = f"https://finance.yahoo.co.jp/quote/{code}"
    try:
        st = _preloaded_state(_get(url))
        fp = st["mainFundPriceBoard"]["fundPrices"]
    except Exception as e:  # noqa: BLE001
        return {**base, "status": f"取得失敗: {type(e).__name__}: {e}"}

    if str(fp.get("code")) != str(code):
        return {**base, "status": f"取得失敗: 要求コード{code}に対し{fp.get('code')}が返った"}

    nav = _num(fp.get("price"))
    if nav is None:
        return {**base, "status": "取得失敗: 基準価額を数値として読めなかった"}

    as_of = _resolve_mmdd(fp.get("updateDate", ""), dt.datetime.now(JST))
    if not as_of:
        return {**base, "status": f"取得失敗: 基準日を解釈できなかった({fp.get('updateDate')!r})"}

    return {
        **base,
        "name_source": fp.get("name"),   # 先方の正式名。取り違え検知用に残す
        "as_of": as_of,
        "nav": nav,
        "chg": _num(fp.get("changePrice")),
        "chg_pct": _num(fp.get("changePriceRate")),
        "nisa_tsumitate": bool(fp.get("isNisaTsumi")),
        "nisa_growth": bool(fp.get("isNisaGrowth")),
        "fetched_at_jst": dt.datetime.now(JST).isoformat(timespec="seconds"),
    }


def fetch_funds(items: list[dict]) -> dict:
    """config.yaml の funds: をまとめて取得。1本失敗しても他は返す。"""
    return {it["code"]: fetch_fund(it["code"], it["name"]) for it in (items or [])}
