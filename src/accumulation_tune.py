"""
閾値を決めるための感度分析。F の「勝率と平均騰落率を出して閾値を調整する」の実体。

  python src/accumulation_tune.py [--layer universe] [--period 10y] [--horizon 20]

やり方:
  1. 全銘柄・全履歴を1回だけ走査して、スパイク1本につき1行の表を作る
     （出来高比・終値位置・④の各条件の合否・確定日からの先行リターン）
  2. その表を条件で切り直して、変種ごとの勝率と平均騰落率を出す

1回の走査で表を作ってから切るので、変種を何通り試しても再取得は起きない。

未来を覗かないための決まり:
  - 集積系の起点は「スパイク日 + followup_days」の終値。④の判定に使った10日ぶんの
    値動きを事前に知っていたことにしない。
  - 売り抜け系の起点はスパイク当日の終値（当日の引けで判定できるため）。
  - horizon 日ぶんの足が無い行は集計から外す。
比較の物差しとして、同じ銘柄・同じ期間の全営業日を起点にしたベースラインを必ず併記する。
これが無いと勝率55%が高いのか低いのか判断できない。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import accumulation as acc  # noqa: E402
import collect  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def scan_stock(code: str, name: str, d: pd.DataFrame, p: acc.Params,
               horizon: int) -> tuple[list[dict], list[float]]:
    """1銘柄ぶんのスパイク表と、ベースライン用の全営業日リターンを返す。"""
    close = d["Close"].values
    rows = []
    start = p.vol_sma_days + 1
    for i in range(start, len(d)):
        vr = d["vol_ratio"].iat[i]
        crp = d["crp"].iat[i]
        if not (vr == vr) or not (crp == crp) or vr < 1.5:
            continue  # 1.5倍未満は感度分析の下限（閾値を下げる方向も見たいので2.0では切らない）

        r: dict = {"code": code, "name": name,
                   "date": d.index[i].strftime("%Y-%m-%d"),
                   "vr": round(float(vr), 4), "crp": round(float(crp), 4)}

        # 売り抜け側: スパイク当日の終値が起点
        j = i + horizon
        r["fwd_from_spike"] = (round(float(close[j] / close[i] - 1) * 100, 4)
                               if j < len(close) else None)

        # 集積側: ④の各条件と、10日後を起点とした先行リターン
        fu = acc.followup(d, i, p)
        if fu.get("state") == "evaluated":
            r["dd"] = fu["drawdown"]["pass"]
            r["gap"] = fu["gap_hold"]["pass"]
            r["rc"] = fu["range_contraction"]["pass"]
            # レンジ収縮は倍率を振りたいので比そのものも持つ
            a = fu["range_contraction"].get("after_mean_pct")
            b = fu["range_contraction"].get("before_mean_pct")
            r["rc_ratio"] = round(a / b, 4) if (a and b) else None
            e = i + p.followup_days
            k = e + horizon
            r["fwd_from_entry"] = (round(float(close[k] / close[e] - 1) * 100, 4)
                                   if k < len(close) else None)
        else:
            r["dd"] = r["gap"] = r["rc"] = r["rc_ratio"] = None
            r["fwd_from_entry"] = None
        rows.append(r)

    base = [float(close[j + horizon] / close[j] - 1) * 100
            for j in range(start, len(close) - horizon)]
    return rows, base


def stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0, "win": None, "mean": None, "median": None}
    a = np.array(vals)
    return {"n": len(a), "win": round(float((a > 0).mean() * 100), 2),
            "mean": round(float(a.mean()), 3),
            "median": round(float(np.median(a)), 3)}


def line(label: str, s: dict, base_win: float, base_mean: float) -> str:
    if not s["n"]:
        return f"{label:<34} n=0"
    return (f"{label:<34} n={s['n']:<6} 勝率={s['win']:>6.2f}% "
            f"({s['win']-base_win:+5.2f}pt)  平均={s['mean']:>7.3f}% "
            f"({s['mean']-base_mean:+6.3f}pt)  中央={s['median']:>7.3f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", default="universe", choices=["core", "universe", "both"])
    ap.add_argument("--period", default="10y")
    # 既定を60にする。20営業日は集積の評価には短すぎることが実測で分かった
    # (20日 -0.63pt / 60日 +2.12pt / 120日 +4.55pt)。docs/accumulation.md 参照。
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--json", default="out/accumulation_tune.json")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    p = acc.Params(cfg)

    targets = []
    if args.layer in ("core", "both"):
        targets += acc.core_targets(cfg, p)
    if args.layer in ("universe", "both"):
        import universe as universe_mod
        cfg["accumulation"]["layers"]["universe"]["enabled"] = True
        u = universe_mod.members(cfg, ROOT / "out")
        if u.get("members") is None:
            print(f"層2を取得できていません: {u.get('status')}")
            return
        have = {t["code"] for t in targets}
        targets += [{"code": m["code"], "name": m["name"]}
                    for m in u["members"] if m["code"] not in have]

    print(f"対象{len(targets)}銘柄 / period={args.period} / horizon={args.horizon}営業日")
    frames, failed = collect.fetch_ohlcv_batch(
        [t["code"] for t in targets], period=args.period, chunk=40)
    print(f"一括取得: 成功{len(frames)} / 失敗{len(failed)}")

    rows, base, excluded = [], [], []
    for t in targets:
        df = frames.get(t["code"])
        if df is None or df.empty:
            excluded.append({"code": t["code"], "reason": "株価を取得できていません"})
            continue
        if len(df) < p.min_history_days:
            excluded.append({"code": t["code"],
                             "reason": f"履歴{len(df)}営業日 < 必要{p.min_history_days}"})
            continue
        d = acc.prepare(df, p)
        r, b = scan_stock(t["code"], t["name"], d, p, args.horizon)
        rows += r
        base += b
    print(f"除外 {len(excluded)}銘柄 / スパイク行 {len(rows)}")

    bs = stats(base)
    print(f"\nベースライン(全営業日起点): n={bs['n']:,} 勝率={bs['win']}% "
          f"平均={bs['mean']}% 中央={bs['median']}%\n")
    bw, bm = bs["win"], bs["mean"]

    def sel(pred, field) -> list[float]:
        return [r[field] for r in rows if r.get(field) is not None and pred(r)]

    print("=== 集積側: ④の条件の効き方 (VolRatio>=2.0, CRP>=0.7) ===")
    cand = lambda r: r["vr"] >= 2.0 and r["crp"] >= 0.7 and r["dd"] is not None
    print(line("候補のみ（④を課さない）", stats(sel(cand, "fwd_from_entry")), bw, bm))
    print(line("④3/3（仕様どおり）",
               stats(sel(lambda r: cand(r) and r["dd"] and r["gap"] and r["rc"],
                         "fwd_from_entry")), bw, bm))
    print(line("押し込み耐性のみ", stats(sel(lambda r: cand(r) and r["dd"],
                                       "fwd_from_entry")), bw, bm))
    print(line("窓維持のみ", stats(sel(lambda r: cand(r) and r["gap"],
                                  "fwd_from_entry")), bw, bm))
    print(line("レンジ収縮のみ", stats(sel(lambda r: cand(r) and r["rc"],
                                    "fwd_from_entry")), bw, bm))
    print(line("押し込み耐性＋窓維持（収縮を外す）",
               stats(sel(lambda r: cand(r) and r["dd"] and r["gap"],
                         "fwd_from_entry")), bw, bm))

    print("\n=== レンジ収縮の倍率を振る (押し込み耐性＋窓維持は課す) ===")
    for mult in (0.7, 0.85, 1.0, 1.2, 1.5, None):
        lab = f"収縮倍率 < {mult}" if mult else "収縮を課さない"
        pred = (lambda r, m=mult: cand(r) and r["dd"] and r["gap"]
                and (m is None or (r["rc_ratio"] is not None and r["rc_ratio"] < m)))
        print(line(lab, stats(sel(pred, "fwd_from_entry")), bw, bm))

    print("\n=== 出来高スパイクの閾値を振る (④は押し込み耐性＋窓維持, CRP>=0.7) ===")
    for v in (1.5, 2.0, 2.5, 3.0, 4.0):
        pred = lambda r, v=v: (r["vr"] >= v and r["crp"] >= 0.7
                               and r["dd"] and r["gap"])
        print(line(f"VolRatio >= {v}", stats(sel(pred, "fwd_from_entry")), bw, bm))

    print("\n=== 終値位置の閾値を振る (VolRatio>=2.0, ④は押し込み耐性＋窓維持) ===")
    for c in (0.6, 0.7, 0.8, 0.9):
        pred = lambda r, c=c: (r["vr"] >= 2.0 and r["crp"] >= c
                               and r["dd"] and r["gap"])
        print(line(f"CRP >= {c}", stats(sel(pred, "fwd_from_entry")), bw, bm))

    print("\n=== 売り抜け側: スパイク当日終値を起点 ===")
    print(line("仕様どおり (VolRatio>=2.0, CRP<=0.3)",
               stats(sel(lambda r: r["vr"] >= 2.0 and r["crp"] <= 0.3,
                         "fwd_from_spike")), bw, bm))
    for c in (0.2, 0.1):
        print(line(f"CRP <= {c} に厳しくする",
                   stats(sel(lambda r, c=c: r["vr"] >= 2.0 and r["crp"] <= c,
                             "fwd_from_spike")), bw, bm))
    for v in (3.0, 4.0):
        print(line(f"VolRatio >= {v}, CRP<=0.3",
                   stats(sel(lambda r, v=v: r["vr"] >= v and r["crp"] <= 0.3,
                             "fwd_from_spike")), bw, bm))

    out = {"horizon": args.horizon, "period": args.period, "layer": args.layer,
           "n_targets": len(targets), "n_excluded": len(excluded),
           "baseline": bs, "n_rows": len(rows), "rows": rows}
    pathlib.Path(args.json).write_text(
        json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n表を書き出し: {args.json}（{len(rows)}行）")


if __name__ == "__main__":
    main()
