"""
ドル円(USD/JPY)の機械計算レイヤー。

このモジュールが返す数値は全て yfinance の実データからの計算結果であり、
取れなかった項目は None + status で出す。前回値や「それらしい値」では絶対に埋めない
（yahoo_jp.py と同じ方針。このリポジトリで過去に繰り返したバグの再発防止）。

取得元:
  - 日足 / 1時間足: yfinance の JPY=X（= USD/JPY。1ドル何円かの表記）
  - 米10年金利: 既に facts["macro"]["^TNX"] で取得済みのものを再利用する（二重取得しない）
  - 日本10年金利: 財務省の公式CSV（下記）。取れなければ日米金利差は「算出不可」。

日本10年金利について:
  yfinance に日本国債10年のティッカーは無い（^TNX に相当するものが存在しない）。
  一次情報は財務省「国債金利情報」の jgbcm.csv:
      https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv
  Shift-JIS・和暦・1974年からの全日次という重いCSVなので、パースは厳格に検査し、
  1つでも想定外があれば値を出さずに status を返す（間違った金利差を出すくらいなら出さない）。
  ※このパーサはリポジトリの開発環境（外部ネットワーク遮断）から実測できていない。
    実機で初回実行したときに status が出るようなら、応答を見て確定させること。

時刻の扱い:
  FXに取引所の引けは無いため「前日」をJSTの暦日で定義する。
  1時間足の集計対象日は hourly.date_jst に必ず明記し、日足の as_of と食い違った場合は
  黙って合わせずに note を出す（どちらかがズレていることを見えるようにするため）。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math
import re

import numpy as np
import pandas as pd
import requests
import yfinance as yf

JST = dt.timezone(dt.timedelta(hours=9))

PAIR = "JPY=X"
NAME = "ドル円"
DAILY_BARS = 260        # 日足の保持本数
HOURLY_DAYS = 5         # 1時間足の取得日数
MIN_DAILY_BARS = 25     # 20MA と 20日高安を出すのに必要な最小本数

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) morning-report/1.0"
TIMEOUT = 25

# 財務省 国債金利情報（一次情報）。列「10年」を使う。
MOF_JGB_CSV = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
# ^TNX が「%表記」である前提が崩れていないかの検査レンジ。
# Yahooは過去に10倍表記(41.5 = 4.15%)だった経緯があり、単位が変わると金利差が静かに壊れる。
US10Y_SANE = (0.0, 20.0)
JP10Y_SANE = (-2.0, 5.0)
JGB_MAX_AGE_DAYS = 14   # CSV最終行がこれより古ければ「古い」として値を出さない


def _f(x, d: int = 4):
    """NaN/Inf を None に落として JSON 安全にする（collect._f と同じ方針）"""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(x) or math.isinf(x)) else round(x, d)


def _jst_index(df: pd.DataFrame) -> pd.DataFrame:
    """indexをJSTのtz-aware に揃える。tz無しはUTCとみなさずそのままJSTとして扱わない —
    yfinanceが返す日足は tz-aware（取引所TZ）か naive(=日付のみ)のどちらか。
    naive は「日付そのもの」なので変換すると1日ズレる。だから naive は触らない。"""
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        df = df.copy()
        df.index = idx.tz_convert(JST)
    return df


def fetch_daily(pair: str = PAIR, bars: int = DAILY_BARS) -> pd.DataFrame:
    """日足。260本を確保するためカレンダー日では余裕を持って取りに行く。"""
    start = (dt.datetime.now(JST).date() - dt.timedelta(days=int(bars * 1.8) + 30)).isoformat()
    df = yf.Ticker(pair).history(start=start, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=["Close"])
    return _jst_index(df).tail(bars)


def fetch_hourly(pair: str = PAIR, days: int = HOURLY_DAYS) -> pd.DataFrame:
    """1時間足。yfinanceの1h足は tz-aware で返るのでJSTへ変換する。"""
    df = yf.Ticker(pair).history(period=f"{days}d", interval="1h", auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=["Close"])
    return _jst_index(df)


# ---------------------------------------------------------------- 日本10年金利
def _wareki_to_date(s: str) -> dt.date | None:
    """'R7.9.19' / 'H31.4.1' / 'S49.9.24' → date。読めなければ None（推測しない）。"""
    m = re.match(r"^\s*([MTSHR])(\d{1,2})\.(\d{1,2})\.(\d{1,2})\s*$", str(s))
    if not m:
        return None
    era, y, mo, da = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
    base = {"M": 1867, "T": 1911, "S": 1925, "H": 1988, "R": 2018}.get(era)
    if base is None or y < 1:
        return None
    try:
        return dt.date(base + y, mo, da)
    except ValueError:
        return None


def fetch_jgb10y(url: str = MOF_JGB_CSV) -> dict:
    """財務省CSVから日本10年国債利回りの最新値を取る。

    厳格に検査し、1つでも想定と違えば値を返さない:
      - ヘッダ行に「基準日」と「10年」が揃っているか
      - 最新の有効行の日付が和暦として解釈できるか
      - その日付が JGB_MAX_AGE_DAYS 日以内か
      - 値が JP10Y_SANE の範囲内か
    """
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        text = r.content.decode("cp932", errors="replace")
    except Exception as e:
        return {"status": f"取得失敗: {type(e).__name__}: {e}", "source_url": url}

    rows = list(csv.reader(io.StringIO(text)))
    hdr_i = col = None
    for i, row in enumerate(rows[:10]):
        cells = [c.strip() for c in row]
        if any(c.startswith("基準日") for c in cells) and "10年" in cells:
            hdr_i, col = i, cells.index("10年")
            break
    if col is None:
        return {"status": "取得失敗: CSVのヘッダに「基準日」「10年」が見つからない（書式変更の可能性）",
                "source_url": url}

    last = None
    for row in rows[hdr_i + 1:]:
        if len(row) <= col:
            continue
        v, d = row[col].strip(), _wareki_to_date(row[0])
        if d is None or v in ("", "-", "*"):
            continue
        try:
            fv = float(v)
        except ValueError:
            continue
        last = (d, fv)
    if last is None:
        return {"status": "取得失敗: CSVに有効な「10年」の行が無い", "source_url": url}

    d, fv = last
    age = (dt.datetime.now(JST).date() - d).days
    if age > JGB_MAX_AGE_DAYS:
        return {"status": f"取得できていません（CSV最終行が{d}で{age}日前。更新停止の可能性）",
                "source_url": url}
    if not (JP10Y_SANE[0] <= fv <= JP10Y_SANE[1]):
        return {"status": f"取得できていません（値 {fv} が想定レンジ外。列ズレの可能性）",
                "source_url": url}
    return {"value_pct": _f(fv, 3), "as_of": d.isoformat(),
            "source": "財務省 国債金利情報", "source_url": url}


def _us10y_from_macro(macro: dict | None) -> dict:
    """既に取得済みの ^TNX を再利用する。単位換算は一切しない（Yahoo表記のまま）。"""
    s = (macro or {}).get("^TNX") or {}
    if not s or s.get("status") or s.get("close") is None:
        return {"status": s.get("status") or "取得できていません（^TNX が facts に無い）"}
    v = float(s["close"])
    if not (US10Y_SANE[0] <= v <= US10Y_SANE[1]):
        return {"status": f"取得できていません（^TNX の値 {v} が%表記として想定レンジ外。"
                          "Yahoo側の単位変更の可能性）", "raw_close": _f(v, 3)}
    return {"value_pct": _f(v, 3), "as_of": s.get("as_of"),
            "source": "Yahoo Finance ^TNX（表記そのまま・単位換算なし）"}


# ---------------------------------------------------------------- 1時間足
def _hourly_prev_day(hourly: pd.DataFrame, today_jst: dt.date,
                     prefer: dt.date | None = None) -> dict:
    """「前日」の1時間足の高安。

    集計日は原則 prefer（= 日足の確定日）に合わせる。日足と1時間足で基準日が違うと
    「前日の高安」が別々の日を指すことになるため。prefer の足が無いときだけ、
    当日(JST)を除いた最新の暦日にフォールバックし、その旨を note に残す。

    FXに取引所の引けは無いので、暦日はJSTで切る。使った日付と本数を必ず返し、
    読み手が「いつの・何本ぶんの高安か」を検証できるようにする
    （週明け月曜など、土曜06:00までの数本しか無い日を掴んだことが見えるように）。
    """
    if hourly is None or hourly.empty:
        return {"status": "取得できていません（1時間足が空）"}
    idx = pd.to_datetime(hourly.index)
    if getattr(idx, "tz", None) is None:
        return {"status": "取得できていません（1時間足にタイムゾーン情報が無く、JSTの暦日に割り当てられない）"}
    days = pd.Index([ts.date() for ts in idx])
    available = sorted({d for d in days if d < today_jst})
    if not available:
        return {"status": f"取得できていません（当日({today_jst})より前の1時間足が無い）"}
    fallback = None
    if prefer is not None and prefer in available:
        target = prefer
    else:
        target = available[-1]
        if prefer is not None:
            fallback = (f"日足の確定日({prefer})の1時間足が取得範囲に無いため、"
                        f"{target} の1時間足で代用した。日足の高安とは別の日を指している。")
    sub = hourly[days == target]
    if sub.empty:
        return {"status": f"取得できていません（{target} の1時間足が無い）"}
    return {
        "fallback_note": fallback,
        "date_jst": target.isoformat(),
        "high": _f(sub["High"].max()),
        "low": _f(sub["Low"].min()),
        "open": _f(sub["Open"].iloc[0]),
        "close": _f(sub["Close"].iloc[-1]),
        "bars": int(len(sub)),
        "first_bar_jst": sub.index[0].strftime("%Y-%m-%d %H:%M"),
        "last_bar_jst": sub.index[-1].strftime("%Y-%m-%d %H:%M"),
        "note": "FXに取引所の引けが無いため、JSTの暦日で区切った1時間足の高安。",
    }


# ---------------------------------------------------------------- 本体
def build(macro: dict | None = None, cfg: dict | None = None, *,
          daily: pd.DataFrame | None = None,
          hourly: pd.DataFrame | None = None, jgb: dict | None = None,
          fetch_daily_fn=None, fetch_hourly_fn=None, fetch_jgb_fn=None) -> dict:
    """ドル円ブロックを組み立てる。

    cfg は config.yaml 全体（fx: セクションだけを見る）。
    daily / hourly / jgb を渡せば取得をスキップする（テスト用の注入口。
    本番は cfg と macro だけ渡して呼ぶ）。
    """
    # デフォルトは呼び出し時にモジュール属性から解決する。def時に束縛すると
    # テストで差し替えた関数が効かない（引数で明示的に渡す経路も残す）。
    fetch_daily_fn = fetch_daily_fn or fetch_daily
    fetch_hourly_fn = fetch_hourly_fn or fetch_hourly
    fetch_jgb_fn = fetch_jgb_fn or fetch_jgb10y

    spec = (cfg or {}).get("fx") or {}
    pair = spec.get("pair") or PAIR
    bars = int(spec.get("daily_bars") or DAILY_BARS)
    hdays = int(spec.get("hourly_days") or HOURLY_DAYS)
    jgb_url = spec.get("jgb10y_csv") or MOF_JGB_CSV

    out: dict = {"code": pair, "name": NAME,
                 "sources": [f"Yahoo Finance (yfinance) {pair} 日足/1時間足"]}

    if daily is None:
        try:
            daily = fetch_daily_fn(pair, bars)
        except Exception as e:
            return {**out, "status": f"取得失敗: {type(e).__name__}: {e}"}
    if daily is None or daily.empty:
        return {**out, "status": "取得できていません（日足が空）"}
    if len(daily) < MIN_DAILY_BARS:
        return {**out, "status": f"算出不可（日足が{len(daily)}本しかなく、20日移動平均・"
                                 f"20日高安を計算できない。必要{MIN_DAILY_BARS}本）"}

    today = dt.datetime.now(JST).date()

    # FXは24時間動くので、当日(JST)の日足は「形成中の足」でありうる。それを前日終値として
    # 出すと、寄り付き前レポートに確定していない値が確定値の顔で載る（このリポジトリで
    # 最も避けたい失敗）。当日付けの足は確定足から外し、別キーで「形成中」と明示する。
    bar_dates = [pd.to_datetime(x).date() for x in daily.index]
    forming = None
    if bar_dates and bar_dates[-1] >= today:
        forming = {"date": bar_dates[-1].isoformat(), "close": _f(daily["Close"].iloc[-1], 3),
                   "note": "当日(JST)付けの足。24時間市場のため形成中の可能性があり、"
                           "確定した終値ではない。前日終値・20MA・20日高安の計算には含めていない。"}
        daily = daily[[d < today for d in bar_dates]]
        if len(daily) < MIN_DAILY_BARS:
            return {**out, "status": f"算出不可（当日分を除いた確定足が{len(daily)}本しかない）",
                    "forming_bar": forming}

    c = daily["Close"]
    last_date = pd.to_datetime(daily.index[-1]).date()
    prev_date = pd.to_datetime(daily.index[-2]).date()

    # 鮮度と欠損。collect.snapshot と同じ考え方（古い足を「最新」と誤読させない）。
    bdays_old = int(np.busday_count(last_date, today)) if last_date <= today else 0
    gap_bdays = int(np.busday_count(prev_date, last_date))
    prev_gap = gap_bdays > 1   # FXは東証カレンダーと無関係なので祝日補正はしない

    ma20 = c.rolling(20).mean().iloc[-1]
    close, prev_close = float(c.iloc[-1]), float(c.iloc[-2])

    out.update({
        "status": None,
        "as_of": last_date.isoformat(),
        "prev_bar_date": prev_date.isoformat(),
        "data_age_bdays": bdays_old,
        "stale": bdays_old >= 2,
        "stale_warning": (f"最新確定足が{bdays_old}営業日前({last_date})。直近の実勢と乖離する場合がある。"
                          if bdays_old >= 2 else None),
        "prev_gap_bdays": gap_bdays,
        "prev_gap_warning": (f"直前の足が{prev_date}で、{last_date}との間に{gap_bdays - 1}営業日ぶんの"
                             "欠損がある。前日比は前営業日との比較にならないため算出しない。"
                             if prev_gap else None),
        # 「前日終値」= 最新の確定日足の終値。寄り付き前レポートで読む値はこれ。
        "close": _f(close, 3),
        "prev_close": _f(prev_close, 3),
        "chg_yen": None if prev_gap else _f(close - prev_close, 3),
        "chg_pct": None if prev_gap else _f((close / prev_close - 1) * 100, 3),
        "ma20": _f(ma20, 3),
        "dev_ma20_pct": _f((close / ma20 - 1) * 100, 3) if ma20 else None,
        "high_20d": _f(daily["High"].tail(20).max(), 3),
        "low_20d": _f(daily["Low"].tail(20).min(), 3),
        "range_note": "20日高安は確定した直近20本の日足の High / Low（当日付けの形成中の足は除く）。",
        "daily_bars": int(len(daily)),
        # 当日(JST)付けの足があった場合だけ入る。参考値であって確定値ではない。
        "forming_bar": forming,
    })

    # 1時間足
    if hourly is None:
        try:
            hourly = fetch_hourly_fn(pair, hdays)
        except Exception as e:
            hourly = None
            out["hourly"] = {"status": f"取得失敗: {type(e).__name__}: {e}"}
    if "hourly" not in out:
        out["hourly"] = _hourly_prev_day(hourly, today, prefer=last_date)
        hd = out["hourly"].get("date_jst")
        if hd and hd != out["as_of"]:
            # 黙って合わせない。どちらかがズレていることを見えるようにする。
            out["hourly"]["mismatch_note"] = (
                out["hourly"].get("fallback_note")
                or f"1時間足の集計日({hd})が日足の最終確定日({out['as_of']})と一致しない。"
                   "日足と1時間足で基準日が異なるため、両者の高安を直接比較しないこと。")

    # 日米金利差
    if jgb is None:
        try:
            jgb = fetch_jgb_fn(jgb_url)
        except Exception as e:
            jgb = {"status": f"取得失敗: {type(e).__name__}: {e}", "source_url": jgb_url}
    us = _us10y_from_macro(macro)
    rates = {"us10y": us, "jp10y": jgb}
    if us.get("value_pct") is not None and jgb.get("value_pct") is not None:
        rates["spread_pt"] = _f(us["value_pct"] - jgb["value_pct"], 3)
        rates["spread_note"] = ("米10年 − 日本10年（%ポイント）。基準日が異なる場合があるため "
                                f"米={us.get('as_of')} / 日={jgb.get('as_of')}。")
    else:
        why = []
        if us.get("value_pct") is None:
            why.append(f"米10年: {us.get('status')}")
        if jgb.get("value_pct") is None:
            why.append(f"日本10年: {jgb.get('status')}")
        rates["spread_pt"] = None
        rates["spread_status"] = "算出不可（" + " / ".join(why) + "）"
    out["rates"] = rates
    if jgb.get("source"):
        out["sources"].append(f"{jgb['source']} {jgb.get('source_url','')}".strip())
    return out
