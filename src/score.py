"""
精度改善ループ用。過去に出したレポートの判定を実績と突き合わせ、的中率を出す。
毎日 out/report_YYYYMMDD.md が溜まる前提。`python src/score.py 30` で直近30営業日を採点。
"""
from __future__ import annotations
import json
import pathlib
import re
import sys

import pandas as pd
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import collect  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
STANCE = ("買い増し", "ホールド", "利益確定", "様子見")


def extract_stance(md: str, name: str) -> str | None:
    """⑧本日の戦略 セクションから銘柄ごとの判定を抜く"""
    sec = md.split("⑧")[-1].split("⑨")[0] if "⑧" in md else md
    for line in sec.split("\n"):
        if name in line:
            for s in STANCE:
                if s in line:
                    return s
    return None


def main(n: int = 30) -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    files = sorted((ROOT / "out").glob("report_*.md"))[-n:]
    px = {h["code"]: collect.fetch_history(h["code"], "2024-01-01") for h in cfg["holdings"]}

    rows = []
    for f in files:
        d = pd.Timestamp(re.search(r"(\d{8})", f.name).group(1))
        md = f.read_text(encoding="utf-8")
        for h in cfg["holdings"]:
            st = extract_stance(md, h["name"])
            df = px[h["code"]]
            fut = df[df.index >= d]
            if st is None or len(fut) < 6:
                continue
            r1 = (fut["Close"].iloc[0] / df[df.index < d]["Close"].iloc[-1] - 1) * 100
            r5 = (fut["Close"].iloc[4] / df[df.index < d]["Close"].iloc[-1] - 1) * 100
            bullish = st in ("買い増し",)
            bearish = st in ("利益確定",)
            hit = (bullish and r5 > 0) or (bearish and r5 < 0) or st in ("ホールド", "様子見")
            rows.append({"date": d.date(), "name": h["name"], "stance": st,
                         "ret_1d_pct": round(r1, 2), "ret_5d_pct": round(r5, 2),
                         "directional_hit": hit if (bullish or bearish) else None})

    df = pd.DataFrame(rows)
    if df.empty:
        print("採点対象なし")
        return
    print(df.to_string(index=False))
    d = df.dropna(subset=["directional_hit"])
    if len(d):
        print("\n方向性判定の的中率:", round(d["directional_hit"].mean() * 100, 1),
              f"% (n={len(d)})")
    print("\n判定別の平均5日リターン:")
    print(df.groupby("stance")["ret_5d_pct"].agg(["count", "mean", "median"]).round(2))
    (ROOT / "out" / "score.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
