"""
「集積の疑いが出た後、実際どうなったか」を集計する検証スクリプト。

  python src/accumulation_backtest.py [--horizon 20] [--codes 7974.T,5803.T] [--json out/xxx.json]

やること: 履歴全体を走査して過去のシグナルを全部拾い、その horizon 営業日後の
騰落率を集計する。勝率と平均騰落率を出す。config.yaml の閾値はこの結果で調整する。

このスクリプトが守ること:
  - 未来を覗かない。シグナル成立日は「スパイク日 + followup_days」であり、
    その日の終値を起点に horizon 日後を測る。スパイク日の終値を起点にすると、
    ④の判定に使った10日ぶんの値動きを「事前に知っていた」ことになる。
  - horizon 日ぶんの足が無いシグナルは集計から外す（途中経過を成績にしない）。
  - 履歴が min_history_days に満たない銘柄は自動的に除外し、除外した旨をログに出す。
  - 比較対象として、同じ期間・同じ銘柄の「全営業日を起点にした horizon 日騰落率」を
    ベースラインとして併記する。これが無いと勝率55%が高いのか低いのか判断できない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import accumulation as acc  # noqa: E402
import collect  # noqa: E402
import earnings as earnings_mod  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def scan_all(d: pd.DataFrame, p: acc.Params, marks: set[int] | None,
             covers_from: str | None) -> list[dict]:
    """履歴全体からシグナルを拾う。analyze() の走査窓を全期間に広げたもの。"""
    sigs = []
    for i in range(p.vol_sma_days + 1, len(d)):
        vr = d["vol_ratio"].iat[i]
        if not (vr == vr) or vr < p.vol_spike_ratio:
            continue
        crp = d["crp"].iat[i]
        if not (crp == crp):
            continue  # H == L。終値位置が定義できないので判定しない
        date = d.index[i].strftime("%Y-%m-%d")
        if marks is None or (covers_from and date < covers_from):
            is_earn = None
        else:
            is_earn = i in marks

        kind = None
        fu = None
        if crp >= p.crp_high:
            fu = acc.followup(d, i, p)
            if fu.get("pass") is True:
                kind = "accumulation"
        elif crp <= p.crp_low and d["wick_from_close"].iat[i] >= p.upper_wick_min:
            kind = "distribution"
        if kind is None:
            continue
        # シグナルが確定する日。集積は④の10日を見終わった時点、
        # 売り抜けはスパイク当日の引けで判定できる。
        entry_i = i + p.followup_days if kind == "accumulation" else i
        if entry_i >= len(d):
            continue
        sigs.append({"i": i, "entry_i": entry_i, "date": date,
                     "entry_date": d.index[entry_i].strftime("%Y-%m-%d"),
                     "kind": kind, "crp": acc._f(crp), "vol_ratio": acc._f(vr),
                     "earnings_spike": is_earn})
    return sigs


def forward_returns(d: pd.DataFrame, sigs: list[dict], horizon: int) -> list[dict]:
    close = d["Close"].values
    out = []
    for s in sigs:
        j = s["entry_i"] + horizon
        if j >= len(close):
            s = dict(s, fwd_pct=None, skipped="horizon日ぶんの足がまだ無い")
            out.append(s)
            continue
        base = close[s["entry_i"]]
        out.append(dict(s, entry_close=acc._f(base),
                        fwd_pct=acc._f((close[j] / base - 1) * 100)))
    return out


def baseline(d: pd.DataFrame, horizon: int, start_i: int) -> dict:
    """同じ銘柄・同じ期間の全営業日を起点にした horizon 日騰落率。比較の物差し。"""
    close = d["Close"].values
    r = [(close[j + horizon] / close[j] - 1) * 100
         for j in range(start_i, len(close) - horizon)]
    if not r:
        return {"n": 0, "win_rate_pct": None, "mean_pct": None, "median_pct": None}
    a = np.array(r)
    return {"n": len(a), "win_rate_pct": acc._f((a > 0).mean() * 100),
            "mean_pct": acc._f(a.mean()), "median_pct": acc._f(np.median(a))}


def summarize(rows: list[dict]) -> dict:
    done = [r for r in rows if r.get("fwd_pct") is not None]
    if not done:
        return {"n": 0, "pending": len(rows), "win_rate_pct": None,
                "mean_pct": None, "median_pct": None,
                "note": "horizon日ぶんの足が揃ったシグナルが1件も無い"}
    a = np.array([r["fwd_pct"] for r in done])
    return {"n": len(a), "pending": len(rows) - len(done),
            "win_rate_pct": acc._f((a > 0).mean() * 100),
            "mean_pct": acc._f(a.mean()), "median_pct": acc._f(np.median(a)),
            "best_pct": acc._f(a.max()), "worst_pct": acc._f(a.min())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=20,
                    help="シグナル確定日から何営業日後の騰落率を見るか")
    ap.add_argument("--codes", default=None, help="カンマ区切り。既定は層1の全銘柄")
    ap.add_argument("--json", default=None, help="結果をこのパスにJSONで書き出す")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    p = acc.Params(cfg)
    targets = acc.core_targets(cfg, p)
    if args.codes:
        want = {c.strip() for c in args.codes.split(",")}
        targets = [t for t in targets if t["code"] in want]

    excluded, per_code, all_rows = [], {}, []
    print(f"horizon={args.horizon}営業日 / 対象{len(targets)}銘柄 / "
          f"閾値 vol>={p.vol_spike_ratio} CRP>={p.crp_high} N={p.followup_days} "
          f"U/D>={p.ud_min}\n")

    for t in targets:
        df = collect.fetch_history(t["code"])
        if df is None or df.empty:
            excluded.append({"code": t["code"], "name": t["name"],
                             "reason": "株価データを取得できていません"})
            print(f"除外 {t['code']} {t['name']}: 株価データを取得できていません")
            continue
        if len(df) < p.min_history_days:
            reason = (f"履歴{len(df)}営業日 < 必要{p.min_history_days}営業日"
                      "（上場が新しく検証に使えない）")
            excluded.append({"code": t["code"], "name": t["name"], "reason": reason})
            print(f"除外 {t['code']} {t['name']}: {reason}")
            continue

        d = acc.prepare(df, p)
        try:
            em = earnings_mod.past_announcements(
                [t["code"]], out_dir=ROOT / "out",
                cache_hours=p.earn_cache_hours, limit=p.earn_tdnet_limit)[t["code"]]
        except Exception as e:
            em = {"status": f"取得失敗: {e}", "announcements": None, "covers_from": None}
        marks, _ = acc.reaction_positions(d, em, p)

        sigs = forward_returns(d, scan_all(d, p, marks, em.get("covers_from")),
                               args.horizon)
        for s in sigs:
            s["code"], s["name"] = t["code"], t["name"]
        all_rows += sigs
        a = [s for s in sigs if s["kind"] == "accumulation"]
        dd = [s for s in sigs if s["kind"] == "distribution"]
        per_code[t["code"]] = {
            "name": t["name"], "history_days": int(len(d)),
            "first_date": d.index[0].strftime("%Y-%m-%d"),
            "accumulation": summarize(a), "distribution": summarize(dd),
            "baseline": baseline(d, args.horizon, p.vol_sma_days + 1),
        }
        print(f"{t['code']:<8}{t['name'][:10]:<12} 履歴{len(d):>5}日  "
              f"集積{len(a):>3}件  売り抜け{len(dd):>3}件")

    acc_rows = [r for r in all_rows if r["kind"] == "accumulation"]
    dist_rows = [r for r in all_rows if r["kind"] == "distribution"]
    result = {
        "as_of": dt.datetime.now(acc.JST).isoformat(timespec="seconds"),
        "horizon_bdays": args.horizon,
        "params": p.as_dict(),
        "n_codes": len(per_code),
        "excluded": excluded,
        "overall": {
            "accumulation": summarize(acc_rows),
            "distribution": summarize(dist_rows),
            # 全銘柄のベースラインは銘柄ごとの勝率の単純平均（銘柄数で割る）。
            # 全シグナルを混ぜた平均ではないので、per_code の baseline と定義が違う。
            "baseline_mean_of_codes": acc._f(np.mean(
                [v["baseline"]["win_rate_pct"] for v in per_code.values()
                 if v["baseline"]["win_rate_pct"] is not None])) if per_code else None,
        },
        "by_code": per_code,
        "signals": all_rows,
    }

    print("\n=== 全体 ===")
    for k, label in (("accumulation", "集積の疑い"), ("distribution", "売り抜けの疑い")):
        s = result["overall"][k]
        if not s["n"]:
            print(f"{label}: 集計できるシグナルなし（未確定{s.get('pending', 0)}件）"
                  f" — {s.get('note', '')}")
            continue
        print(f"{label}: n={s['n']} 勝率={s['win_rate_pct']}% 平均={s['mean_pct']}% "
              f"中央値={s['median_pct']}% 最良={s['best_pct']}% 最悪={s['worst_pct']}%"
              f"（未確定{s['pending']}件は除外）")
    print(f"ベースライン(全営業日起点の勝率・銘柄平均): "
          f"{result['overall']['baseline_mean_of_codes']}%")
    if excluded:
        print(f"\n除外 {len(excluded)}銘柄: "
              + ", ".join(f"{e['code']}({e['reason']})" for e in excluded))

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
        print(f"\n書き出し: {args.json}")


if __name__ == "__main__":
    main()
