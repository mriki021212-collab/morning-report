"""
層2ユニバースを構築して out/universe_jpx_filtered.json に書く。

  python src/universe_build.py

重い（3,600銘柄の株価取得 + 数百銘柄の時価総額照会）ので、週1回のフル走査の前に
1度だけ走らせる。accumulation.py は出来上がったファイルを読むだけ。

フィルタは config.yaml の accumulation.layers.universe.filters にある。
条件を通らなかった銘柄を推測で足すことはしない。取得に失敗した銘柄は
「帯の中かもしれないが確認できない」ので通さない。
"""
from __future__ import annotations

import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import universe  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    d = universe.build_jpx_universe(cfg, ROOT / "out")
    if d.get("members") is None:
        print(f"\n失敗: {d.get('status')}")
        return

    fn = d["funnel"]
    print(f"\n=== 層2ユニバース {d['n']}銘柄 / {d['elapsed_sec']}秒 ===")
    print(f"JPX一覧 {fn['listed_total']}"
          f" → 一次フィルタ {fn['after_primary']}"
          f" → 履歴長・売買代金 {fn['after_liquidity']}"
          f" → 時価総額 {fn['after_market_cap']}")
    if d["members"]:
        caps = [m["market_cap_oku"] for m in d["members"] if m["market_cap_oku"]]
        caps.sort()
        print(f"時価総額: 最小{caps[0]:,.0f} / 中央{caps[len(caps)//2]:,.0f} "
              f"/ 最大{caps[-1]:,.0f} 億円")
        import collections
        ind = collections.Counter(m["industry"] for m in d["members"])
        print("業種上位: " + " / ".join(f"{k}{v}" for k, v in ind.most_common(6)))
        print("\n先頭10銘柄:")
        for m in d["members"][:10]:
            print(f"  {m['code']:<9}{m['name'][:18]:<20}{m['market_cap_oku']:>9,.0f}億円"
                  f"  {m['size']}")
    print(f"\n書き出し: out/universe_jpx_filtered.json")


if __name__ == "__main__":
    main()
