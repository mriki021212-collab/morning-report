"""
層2の規模テスト。一括取得と判定が何銘柄まで現実的に回るかを実測する。

  python src/accumulation_scaletest.py [--mode nikkei225] [--limit N]

出すのは所要時間と失敗率だけ。ここでシグナルの良し悪しは論じない。
「毎朝これを回して落ちないか」を判断するための数字を取る。
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
import time

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import accumulation as acc  # noqa: E402
import collect  # noqa: E402
import universe  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="nikkei225")
    ap.add_argument("--limit", type=int, default=None, help="先頭N銘柄だけで試す")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    ucfg = cfg["accumulation"]["layers"]["universe"]
    # このスクリプトは測定用なので、configの enabled に関係なく走らせる
    ucfg["enabled"] = True
    ucfg["mode"] = args.mode
    p = acc.Params(cfg)
    out = ROOT / "out"

    t0 = time.perf_counter()
    u = universe.members(cfg, out)
    t_list = time.perf_counter() - t0
    if u.get("members") is None:
        print(f"ユニバースを取得できていません: {u.get('status')}")
        return
    codes = [m["code"] for m in u["members"]]
    names = {m["code"]: m["name"] for m in u["members"]}
    if args.limit:
        codes = codes[:args.limit]
    print(f"mode={args.mode} source={u.get('source')} as_of={u.get('as_of')}")
    print(f"銘柄リスト取得: {len(codes)}銘柄 / {t_list:.2f}秒\n")

    f = ucfg["fetch"]
    t0 = time.perf_counter()
    frames, failed = collect.fetch_ohlcv_batch(codes, period=f["period"],
                                               chunk=f["chunk"], pause=f["pause_sec"])
    t_fetch = time.perf_counter() - t0
    print(f"一括取得: 成功{len(frames)} / 失敗{len(failed)} / {t_fetch:.1f}秒 "
          f"({t_fetch/max(len(codes),1)*1000:.0f}ms per銘柄, chunk={f['chunk']}, "
          f"period={f['period']})")
    if failed:
        for c, why in list(failed.items())[:10]:
            print(f"   取得失敗 {c}: {why}")
        if len(failed) > 10:
            print(f"   … 他{len(failed)-10}銘柄")

    lens = sorted(len(d) for d in frames.values())
    short = [c for c, d in frames.items() if len(d) < p.min_history_days]
    print(f"\n履歴長: 最小{lens[0] if lens else 0} / 中央{lens[len(lens)//2] if lens else 0} "
          f"/ 最大{lens[-1] if lens else 0}営業日")
    print(f"履歴不足({p.min_history_days}営業日未満): {len(short)}銘柄 "
          + (f"— {', '.join(short[:8])}" if short else ""))

    # 判定本体（決算日の照会は含めない。TDnetへの負荷は別途測る）
    t0 = time.perf_counter()
    prepared, spiky = {}, []
    for c, df in frames.items():
        if len(df) < p.min_history_days:
            continue
        d = acc.prepare(df, p)
        prepared[c] = d
        s = max(p.vol_sma_days + 1, len(d) - p.scan_days)
        if bool((d["vol_ratio"].iloc[s:] >= p.vol_spike_ratio).any()):
            spiky.append(c)
    t_prep = time.perf_counter() - t0

    t0 = time.perf_counter()
    results = [acc.analyze(c, names.get(c, c), frames[c], None, p, "universe",
                           prepared=prepared[c]) for c in prepared]
    t_analyze = time.perf_counter() - t0

    kinds: dict[str, int] = {}
    for r in results:
        for sp in r.get("spikes", []):
            kinds[sp["kind"]] = kinds.get(sp["kind"], 0) + 1

    print(f"\n指標計算: {len(prepared)}銘柄 / {t_prep:.1f}秒")
    print(f"判定    : {len(results)}銘柄 / {t_analyze:.1f}秒")
    print(f"スパイクを持つ銘柄: {len(spiky)} "
          f"(= 決算日をTDnetに照会する銘柄数。全{len(codes)}銘柄ではない)")
    print("\nスパイク内訳:")
    for k, v in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {k:<22} {v}")

    total = t_list + t_fetch + t_prep + t_analyze
    print(f"\n合計(決算照会を除く): {total:.1f}秒")
    print(f"TDnet照会を足した見込み: +{len(spiky)}リクエスト "
          f"(キャッシュ{p.earn_cache_hours}時間なので週1回のフル走査でのみ発生)")
    print(f"測定: {dt.datetime.now(acc.JST):%Y-%m-%d %H:%M} JST")


if __name__ == "__main__":
    main()
