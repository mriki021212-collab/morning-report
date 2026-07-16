"""
過去チャート類似検索（アナログ分析）。

「上昇確率」をLLMに勘で言わせない。
直近windowの正規化リターン系列を検索キーにして過去20年超の全窓と距離を測り、
最も似ていた上位k局面の「その後horizon日の実際のリターン」を集計して
実証的な確率分布として返す。これは推測ではなく過去の頻度。
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def _z(v: np.ndarray) -> np.ndarray:
    s = v.std()
    return (v - v.mean()) / s if s > 0 else v - v.mean()


# アナログ分析が統計的に成立する最低条件
MIN_YEARS = 8            # 最低8年（≒1960営業日）の履歴
MIN_CANDIDATES = 800     # 最低800個の候補窓
MAX_TOPK_RATIO = 0.02    # top_k は候補窓の2%以下（=上位2%だけを「類似」と呼ぶ）


def find_analogs(df: pd.DataFrame, window=60, horizon=20, top_k=20,
                 min_history="2005-01-01", exclude_recent=250) -> dict:
    d = df[df.index >= pd.Timestamp(min_history)]
    ret = np.log(d["Close"]).diff().fillna(0).values
    idx = d.index

    n = len(ret)
    last_valid = n - horizon - exclude_recent
    n_cands = max(0, last_valid - window)

    # --- ゲート: 統計的に無意味な結果を「それらしく」返さないための門番 ---
    if n < MIN_YEARS * 245:
        return {"status": "現時点では確認できない",
                "reason": f"履歴不足: {n}営業日 (≒{n/245:.1f}年) < 必要 {MIN_YEARS}年",
                "history_days": n}
    if n_cands < MIN_CANDIDATES:
        return {"status": "現時点では確認できない",
                "reason": f"候補窓不足: {n_cands}個 < 必要 {MIN_CANDIDATES}個",
                "history_days": n}
    if top_k > n_cands * MAX_TOPK_RATIO:
        top_k = max(5, int(n_cands * MAX_TOPK_RATIO))

    query = _z(ret[-window:])
    cands = []
    for s in range(window, last_valid):
        seg = _z(ret[s - window:s])
        dist = float(np.linalg.norm(seg - query)) / np.sqrt(window)
        corr = float(np.corrcoef(seg, query)[0, 1])
        cands.append((dist, corr, s))

    cands.sort(key=lambda x: x[0])
    top = cands[:top_k]

    rows, fwd = [], []
    for dist, corr, s in top:
        f = float(np.exp(ret[s:s + horizon].sum()) - 1) * 100
        fwd.append(f)
        rows.append({
            "date": idx[s - 1].strftime("%Y-%m-%d"),
            "similarity_pct": round(max(0.0, corr) * 100, 1),   # 相関ベースの一致率
            "distance": round(dist, 4),
            f"fwd_{horizon}d_return_pct": round(f, 2),
        })

    fwd = np.array(fwd)
    return {
        "window": window,
        "horizon": horizon,
        "sample_start": idx[0].strftime("%Y-%m-%d"),
        "sample_end": idx[-1].strftime("%Y-%m-%d"),
        "n_candidates_scanned": len(cands),
        "top_k_used": top_k,
        "history_days": n,
        "history_years": round(n / 245, 1),
        "top_matches": rows,
        "empirical_up_prob_pct": round(float((fwd > 0).mean()) * 100, 1),
        "empirical_down_prob_pct": round(float((fwd <= 0).mean()) * 100, 1),
        "median_fwd_return_pct": round(float(np.median(fwd)), 2),
        "mean_fwd_return_pct": round(float(fwd.mean()), 2),
        "p10_fwd_return_pct": round(float(np.percentile(fwd, 10)), 2),
        "p90_fwd_return_pct": round(float(np.percentile(fwd, 90)), 2),
        "note": "これは過去の類似局面における実測頻度であり、将来予測ではない。",
    }


def next_day_prob(df: pd.DataFrame, lookback=1000) -> dict:
    """翌営業日の単純な上昇頻度（ベースレート）。確率の基準線として提示する。"""
    r = df["Close"].pct_change().dropna().tail(lookback)
    if len(r) < 200:
        return {"status": "現時点では確認できない"}
    return {
        "lookback_days": int(len(r)),
        "base_up_rate_pct": round(float((r > 0).mean()) * 100, 1),
        "avg_abs_daily_move_pct": round(float(r.abs().mean()) * 100, 2),
    }
