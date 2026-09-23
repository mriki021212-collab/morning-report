"""
ドル円(USD/JPY)の機械計算レイヤー。

このモジュールが返す数値は全て yfinance の実データからの計算結果であり、
取れなかった項目は None + status で出す。前回値や「それらしい値」では絶対に埋めない
（yahoo_jp.py と同じ方針。このリポジトリで過去に繰り返したバグの再発防止）。

取得元:
  - 1時間足: yfinance の JPY=X（= USD/JPY。1ドル何円かの表記）。日足もここから組み立てる。

Yahoo の JPY=X 日足を使わない理由（2026-09-22 に実データで確認）:
  確定した日足の Close が、その足の終値ではなく「始値の直後の値」になっている。
  直近30本すべてで Close ≈ Open ≈ 最初の1時間足の終値で、実際の引け（窓の最後の1時間足の
  終値 = 翌日足の Open）とは最大約2.8円ずれていた（例: 9/03 日足Close 158.923 / 実際 156.121）。
  Yahoo 自身の quote の previousClose(157.311) は1時間足から組んだ値と一致し、日足Close(157.046)
  とは一致しなかった。つまり日足の Close を「前日終値」として出すと、もっともらしい誤値になる。
  形成中の足だけは Close が生きているため、当日だけ見ていると気づけない。
  そこで日足は Yahoo と同じ区切り（Europe/London の暦日 = 夏時間中は JST 08:00 始まり）で
  1時間足を束ねて作り、窓が閉じていて中身が揃っているものだけを確定足とする。
  - 米10年金利: 既に facts["macro"]["^TNX"] で取得済みのものを再利用する（二重取得しない）
  - 日本10年金利: 財務省の公式CSV（下記）。取れなければ日米金利差は「算出不可」。

日本10年金利について:
  yfinance に日本国債10年のティッカーは無い（^TNX に相当するものが存在しない）。
  一次情報は財務省「国債金利情報」の jgbcm.csv:
      https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv
  Shift-JIS(cp932)・和暦のCSVなので、パースは厳格に検査し、
  1つでも想定外があれば値を出さずに status を返す（間違った金利差を出すくらいなら出さない）。
  2026-09-22 に実機で確認した応答: 当月分だけの約1.5KB（1行目「国債金利情報 (令和8年9月)」、
  2行目がヘッダ「基準日,1年,…,10年,…」、末尾に空行と「※最新のcsvデータが…」の注記行）。
  公表は1営業日ほど遅れる（9/22 時点の最終行は R8.9.17）。当月分しか無いので、月初で当月の
  行がまだ無い日は「有効な行が無い」になり、金利差は算出不可になる（前月値で埋めない）。

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
HOURLY_DAYS = 60        # 1時間足の取得日数。確定日足を25本以上組めるだけ取る（yfinanceの1h上限は730日）
MIN_DAILY_BARS = 25     # 20MA と 20日高安を出すのに必要な最小本数
# Yahoo の FX 日足の区切り。history_metadata の exchangeTimezoneName が Europe/London で、
# 日足の index は London 00:00。夏時間/冬時間で JST 08:00 / 09:00 始まりが自動で切り替わる。
DAY_TZ = "Europe/London"
# 窓の中身が揃っているかの検査。NYクローズ(金曜は土曜 JST 05:00〜06:00 台で終わる)を考え、
# 最後の1時間足の開始が窓の終わりの3時間前より後であること、本数が20本以上あることを要求する。
LAST_BAR_SLACK = pd.Timedelta(hours=3)
MIN_HOURLY_PER_DAY = 20

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


def fetch_hourly(pair: str = PAIR, days: int = HOURLY_DAYS) -> pd.DataFrame:
    """1時間足。yfinanceの1h足は tz-aware で返るのでJSTへ変換する。"""
    df = yf.Ticker(pair).history(period=f"{days}d", interval="1h", auto_adjust=False)
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.dropna(subset=["Close"])
    return _jst_index(df)


def daily_from_hourly(hourly: pd.DataFrame, now: dt.datetime) -> tuple[pd.DataFrame, dict | None, list]:
    """1時間足を Yahoo と同じ区切り(London暦日)で束ねて日足にする。

    返り値: (確定足, 形成中の足 or None, 中身が揃っていない確定窓のリスト)
      - 確定足: 窓の終わり(翌London 00:00)を now が過ぎていて、中身が揃っているもの。
        index は London の暦日(date)。Close は窓の最後の1時間足の終値。
      - 形成中: 窓がまだ閉じていない足。確定値ではないので計算には使わない。
      - 欠け: 窓は閉じているが本数不足 or 最後の足が早すぎる。値は出さず日付だけ返す。
    週末(London の土日)に紛れ込む数本は Yahoo の日足にも存在しないので捨てる。
    """
    if hourly is None or hourly.empty:
        return pd.DataFrame(), None, []
    idx = pd.to_datetime(hourly.index)
    if getattr(idx, "tz", None) is None:
        raise ValueError("1時間足にタイムゾーン情報が無く、日足の区切りに割り当てられない")
    lon = idx.tz_convert(DAY_TZ)
    keys = pd.Index([ts.date() for ts in lon])
    rows, forming, incomplete = [], None, []
    for d in sorted(set(keys)):
        if d.weekday() >= 5:
            continue
        sub = hourly[keys == d]
        start = pd.Timestamp(d).tz_localize(DAY_TZ)
        end = (pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(DAY_TZ)
        win = f"{start.tz_convert(JST):%Y-%m-%d %H:%M}〜{end.tz_convert(JST):%Y-%m-%d %H:%M} JST"
        last_start = pd.Timestamp(sub.index[-1])
        if pd.Timestamp(now) < end:
            forming = {"date": d.isoformat(), "close": _f(sub["Close"].iloc[-1], 3),
                       "last_bar_jst": last_start.tz_convert(JST).strftime("%Y-%m-%d %H:%M"),
                       "window_jst": win,
                       "note": "窓がまだ閉じていない足。24時間市場のため形成中で、確定した終値ではない。"
                               "前日終値・20MA・20日高安の計算には含めていない。"}
            continue
        if len(sub) < MIN_HOURLY_PER_DAY or last_start < end - LAST_BAR_SLACK:
            incomplete.append(f"{d}（{len(sub)}本、最終 {last_start.tz_convert(JST):%m-%d %H:%M} JST）")
            continue
        rows.append({"date": d, "Open": sub["Open"].iloc[0], "High": sub["High"].max(),
                     "Low": sub["Low"].min(), "Close": sub["Close"].iloc[-1],
                     "bars": len(sub), "window_jst": win})
    daily = pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()
    return daily, forming, incomplete


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
    # 鮮度は snapshot が既に測っている。ここで測り直さず、そのまま持ち上げる。
    # 古い値を黙って金利差に使うと、差だけが実勢から離れる（実測 2026-09-23: Yahoo に
    # 9/22 の ^TNX 足が無く、2営業日前の 9/21 が最新だった）。
    return {"value_pct": _f(v, 3), "as_of": s.get("as_of"),
            "data_age_bdays": s.get("data_age_bdays"),
            "stale": bool(s.get("stale")),
            "stale_warning": s.get("stale_warning"),
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
          hourly: pd.DataFrame | None = None, jgb: dict | None = None,
          now: dt.datetime | None = None,
          fetch_hourly_fn=None, fetch_jgb_fn=None) -> dict:
    """ドル円ブロックを組み立てる。

    cfg は config.yaml 全体（fx: セクションだけを見る）。
    hourly / jgb / now を渡せば取得・時計をスキップする（テスト用の注入口。
    本番は cfg と macro だけ渡して呼ぶ）。
    """
    # デフォルトは呼び出し時にモジュール属性から解決する。def時に束縛すると
    # テストで差し替えた関数が効かない（引数で明示的に渡す経路も残す）。
    fetch_hourly_fn = fetch_hourly_fn or fetch_hourly
    fetch_jgb_fn = fetch_jgb_fn or fetch_jgb10y

    spec = (cfg or {}).get("fx") or {}
    pair = spec.get("pair") or PAIR
    hdays = int(spec.get("hourly_days") or HOURLY_DAYS)
    jgb_url = spec.get("jgb10y_csv") or MOF_JGB_CSV
    now = now or dt.datetime.now(JST)
    today = now.astimezone(JST).date()

    out: dict = {"code": pair, "name": NAME,
                 "sources": [f"Yahoo Finance (yfinance) {pair} 1時間足（日足も1時間足から集計）"]}

    if hourly is None:
        try:
            hourly = fetch_hourly_fn(pair, hdays)
        except Exception as e:
            return {**out, "status": f"取得失敗: {type(e).__name__}: {e}"}
    try:
        daily, forming, incomplete = daily_from_hourly(hourly, now)
    except ValueError as e:
        return {**out, "status": f"取得できていません（{e}）"}
    if daily.empty:
        return {**out, "status": "取得できていません（1時間足が空、または確定した日足を組めない）",
                "forming_bar": forming}
    if len(daily) < MIN_DAILY_BARS:
        return {**out, "status": f"算出不可（確定日足が{len(daily)}本しかなく、20日移動平均・"
                                 f"20日高安を計算できない。必要{MIN_DAILY_BARS}本）",
                "forming_bar": forming}
    # 計算に使う直近21本(前日比の1本 + 20MA/20日高安の20本)の範囲に中身の欠けた窓があると、
    # それを飛ばして1本古い足を黙って混ぜることになる。その場合は値を出さない。
    span_start = daily.index[-21]
    bad = [s for s in incomplete if dt.date.fromisoformat(s[:10]) >= span_start]
    if bad:
        return {**out, "status": "算出不可（1時間足が揃っていない日があり、その日の確定足を組めない: "
                                 + " / ".join(bad) + "）", "forming_bar": forming}

    c = daily["Close"]
    last_date = daily.index[-1]
    prev_date = daily.index[-2]

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
        "range_note": "20日高安は確定した直近20本の日足の High / Low（形成中の足は除く）。",
        # 日足は1時間足からの集計。as_of の日付が指す実際の時間帯をここに明記する
        # （JSTの暦日ではなく London 00:00 区切り。夏時間中は JST 08:00〜翌08:00）。
        "as_of_window_jst": daily["window_jst"].iloc[-1],
        "close_basis": ("1時間足を London 00:00 区切り(Yahooの日足と同じ区切り)で束ねた日足の、"
                        "窓の最後の1時間足の終値。Yahoo の JPY=X 日足の Close は始値直後の値に"
                        "なっているため使っていない。"),
        "daily_bars": int(len(daily)),
        # 窓がまだ閉じていない足があった場合だけ入る。参考値であって確定値ではない。
        "forming_bar": forming,
    })

    # 1時間足（前日のJST暦日の高安）。日足と同じ取得結果を使う。
    out["hourly"] = _hourly_prev_day(hourly, today, prefer=last_date)
    hd = out["hourly"].get("date_jst")
    if hd and hd != out["as_of"]:
        # 黙って合わせない。どちらかがズレていることを見えるようにする。
        out["hourly"]["mismatch_note"] = (
            out["hourly"].get("fallback_note")
            or f"1時間足の集計日({hd})が日足の最終確定日({out['as_of']})と一致しない。"
               "日足と1時間足で基準日が異なるため、両者の高安を直接比較しないこと。")
    elif not out["hourly"].get("status"):
        # 日付は同じでも、日足は London 00:00 区切り、1時間足は JST の暦日で切っている。
        # 高安が同じ時間帯のものだと誤読されないよう、両方の窓を明示する。
        out["hourly"]["window_note"] = (
            f"この高安は {hd} の JST暦日(00:00〜24:00)。日足 {out['as_of']} の窓は "
            f"{out['as_of_window_jst']} で、区切りが異なる。")

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
        # 片方でも古ければ、差そのものが実勢から離れる。値は出すが黙って出さない。
        old = [w for w in (us.get("stale_warning"),
                           (f"日本10年の基準日が{jgb.get('as_of')}（財務省の公表は1営業日ほど遅れる）。"
                            if jgb.get("as_of") and jgb["as_of"] != (us.get("as_of") or jgb["as_of"])
                            else None)) if w]
        if old:
            rates["spread_warning"] = "金利差の基準日に注意: " + " / ".join(old)
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
