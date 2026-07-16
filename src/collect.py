"""
決定論的データ収集レイヤー。
ここで計算した数値だけをLLMに渡す。LLMには数値を作らせない。
"""
from __future__ import annotations
import datetime as dt
import math
import numpy as np
import pandas as pd
import yfinance as yf

JST = dt.timezone(dt.timedelta(hours=9))


def _f(x):
    """NaN/Inf を None に落として JSON 安全にする"""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(x) or math.isinf(x)) else round(x, 4)


def fetch_history(code: str, start: str = "2004-01-01") -> pd.DataFrame:
    df = yf.Ticker(code).history(start=start, auto_adjust=False)
    if df.empty:
        return df
    df = df.dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


# ---------- テクニカル（全て自前計算 = 再現可能） ----------
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def macd(close: pd.Series, fast=12, slow=26, sig=9):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    line = ema_f - ema_s
    signal = line.ewm(span=sig, adjust=False).mean()
    return line, signal, line - signal


def candle_pattern(df: pd.DataFrame) -> str:
    """直近1本の単純な形状ラベル（推測ではなく形状の記述のみ）"""
    o, h, l, c = (df[k].iloc[-1] for k in ("Open", "High", "Low", "Close"))
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return "判定不能"
    upper, lower = h - max(o, c), min(o, c) - l
    if body / rng < 0.1:
        return "同時線（寄引同値に近い）"
    if lower > body * 2 and upper < body:
        return "下ヒゲ長（陽の"+("大" if c > o else "")+"カラカサ型）"
    if upper > body * 2 and lower < body:
        return "上ヒゲ長（トンカチ型）"
    if body / rng > 0.7:
        return "大陽線" if c > o else "大陰線"
    return "小陽線" if c > o else "小陰線"


def support_resistance(df: pd.DataFrame, lookback: int = 120, bins: int = 40):
    """出来高加重の価格帯別売買代金からS/R候補を抽出（出来高プロファイル）"""
    d = df.tail(lookback)
    if d.empty:
        return None, None
    typical = (d["High"] + d["Low"] + d["Close"]) / 3
    hist, edges = np.histogram(typical, bins=bins, weights=d["Volume"])
    centers = (edges[:-1] + edges[1:]) / 2
    last = d["Close"].iloc[-1]
    below = centers[(centers < last) & (hist > 0)]
    above = centers[(centers > last) & (hist > 0)]
    hb = hist[(centers < last) & (hist > 0)]
    ha = hist[(centers > last) & (hist > 0)]
    sup = _f(below[np.argmax(hb)]) if len(below) else None
    res = _f(above[np.argmax(ha)]) if len(above) else None
    return sup, res


def snapshot(code: str, name: str, df: pd.DataFrame) -> dict:
    if df.empty or len(df) < 210:
        return {"code": code, "name": name, "status": "現時点では確認できない（履歴不足）"}
    c = df["Close"]
    ml, ms, mh = macd(c)
    ma25, ma75, ma200 = (c.rolling(n).mean() for n in (25, 75, 200))
    sup, res = support_resistance(df)
    vol = df["Volume"]

    return {
        "code": code,
        "name": name,
        "as_of": df.index[-1].strftime("%Y-%m-%d"),
        "close": _f(c.iloc[-1]),
        "prev_close": _f(c.iloc[-2]),
        "chg_pct": _f((c.iloc[-1] / c.iloc[-2] - 1) * 100),
        "open": _f(df["Open"].iloc[-1]),
        "high": _f(df["High"].iloc[-1]),
        "low": _f(df["Low"].iloc[-1]),
        "volume": _f(vol.iloc[-1]),
        "volume_vs_20d_avg_pct": _f((vol.iloc[-1] / vol.tail(20).mean() - 1) * 100),
        "rsi14": _f(rsi(c).iloc[-1]),
        "macd": _f(ml.iloc[-1]),
        "macd_signal": _f(ms.iloc[-1]),
        "macd_hist": _f(mh.iloc[-1]),
        "macd_cross": ("ゴールデンクロス" if mh.iloc[-1] > 0 >= mh.iloc[-2]
                       else "デッドクロス" if mh.iloc[-1] < 0 <= mh.iloc[-2] else "なし"),
        "ma25": _f(ma25.iloc[-1]),
        "ma75": _f(ma75.iloc[-1]),
        "ma200": _f(ma200.iloc[-1]),
        "dev_ma25_pct": _f((c.iloc[-1] / ma25.iloc[-1] - 1) * 100),
        "dev_ma75_pct": _f((c.iloc[-1] / ma75.iloc[-1] - 1) * 100),
        "dev_ma200_pct": _f((c.iloc[-1] / ma200.iloc[-1] - 1) * 100),
        "high_60d": _f(df["High"].tail(60).max()),
        "low_60d": _f(df["Low"].tail(60).min()),
        "high_52w": _f(df["High"].tail(250).max()),
        "low_52w": _f(df["Low"].tail(250).min()),
        "atr14_pct": _f((df["High"] - df["Low"]).tail(14).mean() / c.iloc[-1] * 100),
        "hv20_annual_pct": _f(c.pct_change().tail(20).std() * math.sqrt(250) * 100),
        "candle": candle_pattern(df),
        "support_vp": sup,
        "resistance_vp": res,
    }


# 引け時刻がSOX(米国)より前に確定する市場のサフィックス。
# ここを間違えると相関が静かに壊れる。判定基準はサフィックスではなくタイムゾーン。
_ASIA = (".T", ".KS", ".KQ", ".TW", ".TWO", ".HK", ".SS", ".SZ", ".SI", ".AX", ".NS", ".BO")


def is_asia(code: str) -> bool:
    return code.upper().endswith(_ASIA)


def market_lag(driver: str, follower: str) -> int:
    """
    driver の終値が follower の終値より「後」に確定するなら 1、同時か前なら 0。

    米国(D)引け = アジア(D)引けの約14時間後。
    → アジア株が反応できるのは 米国(D-1) なので lag=1。
    アジア→米国、米国→米国、アジア→アジア はすべて同日比較でよい（lag=0）。
    """
    return 1 if (not is_asia(driver) and is_asia(follower)) else 0


def live_quote(code: str) -> dict | None:
    """
    分足で「現在値」と「その値がいつのものか」を取る。

    日足の日付だけでは、そのバーが確定済みか形成中かを区別できない。
    先物は24時間動くので、日付が現物と同じでも中身はライブでありうる。
    比較の可否はタイムスタンプで判定する。
    """
    try:
        df = yf.Ticker(code).history(period="2d", interval="1m")
    except Exception:
        return None
    if df.empty:
        return None
    ts = pd.to_datetime(df.index[-1])
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return {"price": _f(df["Close"].iloc[-1]), "ts_jst": ts.tz_convert(JST).isoformat()}


def correlation(a: pd.Series, b: pd.Series, n: int = 60, lag: int = 0) -> float | None:
    """
    a を lag 営業日ぶん後ろにずらして b と相関を取る。

    lag=1 の意味: 米国市場(a)の D-1 終値 と 日本株(b)の D 終値 を突き合わせる。
    SOXのD終値は日本時間D+1の朝5時に確定するため、日本株のD終値がそれに反応するのは
    物理的に不可能。同日比較(lag=0)は無意味な相関を返すので、対米指標は必ず lag=1。
    """
    ra, rb = a.pct_change(), b.pct_change()
    if lag:
        ra = ra.shift(lag)
    x = pd.concat([ra, rb], axis=1).dropna().tail(n)
    if len(x) < 20:
        return None
    return _f(x.iloc[:, 0].corr(x.iloc[:, 1]))
