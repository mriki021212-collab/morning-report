"""
過去日の再現用フィクスチャを作る（ネットワークが必要。1回だけ実行して結果をコミットする）。

    .venv\\Scripts\\python.exe tests\\make_fixtures.py

保存するもの（tests/fixtures/<case>/）:
  - Googleニュース検索RSS の生XML（after:/before: で期間指定）… SOURCES の gnews 型と、
    既存⑥-B と同じ銘柄クエリ（news.HOLDING_QUERIES / SECTOR_QUERIES）
  - TDnet（やのしんWEB-API）の生JSON … 監視銘柄の開示を遡って取得
  - market.json … その朝に実際に生成された out/facts_YYYYMMDD.json から、
    判定に使うキーだけを抜き出したもの（値は一切加工しない）
  - index.json … URL → ファイル名の対応。テストはこれで fetcher を差し替える

再現できないもの（記録だけ残す）:
  - 日銀/FRB/財務省/金融庁/NHK/日経(wor.jp)/Investing.com の RSS は「現在の最新分」しか
    返さないため、過去日を取得できない。index.json の not_reproducible に列挙する。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sys
import urllib.parse

import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import impact  # noqa: E402
import news  # noqa: E402

CASES = {
    # キオクシアの米Viasat特許訴訟の陪審評決（約370億円）。2026-07-17(金)。
    # 次の営業日の朝 = 2026-07-21(火) 08:30（07-20 は海の日）。
    # 市場データ: その朝相当に生成された out/facts_20260720.json（285A の 07-17 終値まで反映済み）
    "20260717_kioxia": {"now": "2026-07-21T08:30:00+09:00", "after": "2026-07-16",
                        "before": "2026-07-22", "facts": "facts_20260720.json"},
    # 米中間選挙の結果を受けたAI・半導体売り（日経平均 3,643円安・−5.96%）。2026-11-06(金)。
    # 次の営業日の朝 = 2026-11-09(月) 08:30。市場データ: out/facts_20261109.json
    "20261106_midterm": {"now": "2026-11-09T08:30:00+09:00", "after": "2026-11-05",
                         "before": "2026-11-10", "facts": "facts_20261109.json"},
}
MARKET_KEYS = ("macro", "holdings", "watch", "sector", "overseas_semis", "sector_groups",
               "econ_calendar", "generated_at_jst")


def replay_sources() -> list[dict]:
    """再現に使うソース = SOURCES の gnews 型 + 既存⑥-B の銘柄クエリ（同じく gnews）。"""
    out = [s for s in impact.SOURCES if s["kind"] == "gnews"]
    for name, q in {**news.HOLDING_QUERIES, **news.SECTOR_QUERIES}.items():
        out.append({"id": f"b_{name}", "layer": "other", "media": "Googleニュース", "kind": "gnews", "q": q})
    return out


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    codes = [h["code"] for h in cfg["holdings"]] + [w["code"] for w in cfg["watch"]]
    for case, spec in CASES.items():
        d = ROOT / "tests" / "fixtures" / case
        d.mkdir(parents=True, exist_ok=True)
        after, before = dt.date.fromisoformat(spec["after"]), dt.date.fromisoformat(spec["before"])
        index = {"case": case, "now": spec["now"], "after": spec["after"], "before": spec["before"],
                 "fetched_at": dt.datetime.now(impact.JST).isoformat()[:19], "urls": {}, "results": [],
                 "not_reproducible": [s["id"] for s in impact.SOURCES if s["kind"] != "gnews"]
                 + ["news(既存⑥-C wor.jp)"]}
        for s in replay_sources():
            url = impact.source_url(s, after, before)
            st, body, err = impact.fetch_raw(url, 10)
            fn = f"gn_{hashlib.sha1(url.encode()).hexdigest()[:10]}.xml"
            n = len(impact.parse_feed(s, body)) if body and not err else 0
            index["results"].append({"id": s["id"], "q": s.get("q"), "http": st, "error": err, "n": n})
            if body and not err:
                (d / fn).write_bytes(body)
                index["urls"][url] = fn
            print(case, s["id"], st, err, n)
        # TDnet: 監視銘柄の開示を遡って取る（期間で絞るのはテスト側）
        allitems = []
        for c in codes:
            url = f"{impact_tdnet_base()}/{c.replace('.T', '')}.json?limit=300"
            r = requests.get(url, timeout=20, headers={"User-Agent": "morning-report/1.0"})
            got = r.json().get("items", []) if r.ok else []
            allitems += got
            index["results"].append({"id": f"tdnet_{c}", "http": r.status_code, "n": len(got)})
            print(case, "tdnet", c, r.status_code, len(got))
        # 再現に要るのは期間内の開示だけ。全履歴を保存すると数MBになるので after〜before に絞る
        keep = [x for x in allitems
                if spec["after"] <= (x.get("Tdnet", x).get("pubdate") or "")[:10] <= spec["before"]]
        (d / "tdnet.json").write_text(json.dumps({"items": keep}, ensure_ascii=False), encoding="utf-8")
        f = json.loads((ROOT / "out" / spec["facts"]).read_text(encoding="utf-8"))
        market = {k: f.get(k) for k in MARKET_KEYS}
        market["_source_file"] = f"out/{spec['facts']}"
        (d / "market.json").write_text(json.dumps(market, ensure_ascii=False, indent=1), encoding="utf-8")
        (d / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def impact_tdnet_base() -> str:
    import tdnet
    return tdnet.BASE + ""


if __name__ == "__main__":
    main()
