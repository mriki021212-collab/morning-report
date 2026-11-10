"""
⑪ 株価インパクトニュースのテスト。pytest 不要（このリポジトリにはテスト基盤が無いため）。

    .venv\\Scripts\\python.exe tests\\test_impact.py          # 1〜5（オフライン・フィクスチャ使用）
    .venv\\Scripts\\python.exe tests\\test_impact.py --live   # 6 も実行（実ソースに取りに行く）

各項目は 合格 / 不合格 / 再現不可 を印字し、不合格が1つでもあれば終了コード1。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
import time

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
import impact  # noqa: E402
from make_fixtures import CASES, replay_sources  # noqa: E402

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
FIX = ROOT / "tests" / "fixtures"
RESULTS: list[tuple[str, str, str]] = []


def record(name: str, verdict: str, detail: str) -> None:
    RESULTS.append((name, verdict, detail))
    print(f"[{verdict}] {name}: {detail}")


# ---------------------------------------------------------------- フィクスチャ読込
def fixture_fetcher(case: str):
    idx = json.loads((FIX / case / "index.json").read_text(encoding="utf-8"))

    def f(url, timeout):
        fn = idx["urls"].get(url)
        if not fn:
            return None, None, "フィクスチャに無いURL"
        return 200, (FIX / case / fn).read_bytes(), None
    return f


def replay_facts(case: str, now: dt.datetime) -> dict:
    market = json.loads((FIX / case / "market.json").read_text(encoding="utf-8"))
    raw = json.loads((FIX / case / "tdnet.json").read_text(encoding="utf-8"))
    items = []
    for row in raw.get("items", []):
        d = row.get("Tdnet", row)
        try:
            when = dt.datetime.strptime(d.get("pubdate", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=impact.JST)
        except ValueError:
            continue
        # 既存 tdnet.fetch と同じ「直近48時間」
        if not (now - dt.timedelta(hours=48) <= when <= now):
            continue
        items.append({"code": (d.get("company_code") or "")[:4], "company": d.get("company_name", ""),
                      "title": d.get("title", ""), "url": d.get("document_url", ""),
                      "time": when.strftime("%Y-%m-%d %H:%M")})
    facts = dict(market)
    facts["tdnet"] = {"status": "ok" if items else "直近に開示なし", "items": items}
    # 既存⑥-B の銘柄クエリは replay_sources() 側で同じ検索語を after/before 付きで取っている
    facts["news"] = {"status": "ok", "holdings": [], "sector": [], "macro": [], "errors": []}
    return facts


def run_case(case: str, **kw) -> dict:
    spec = CASES[case]
    now = dt.datetime.fromisoformat(spec["now"])
    facts = replay_facts(case, now)
    return impact.build(facts, CFG, now=now, fetcher=kw.pop("fetcher", fixture_fetcher(case)),
                        sources=replay_sources(), use_llm=kw.pop("use_llm", False),
                        after=dt.date.fromisoformat(spec["after"]),
                        before=dt.date.fromisoformat(spec["before"]), **kw)


def all_text(res: dict) -> str:
    md = "\n".join(impact.render_md(res))
    dc = json.dumps(impact.discord_fields(res), ensure_ascii=False)
    db = json.dumps(impact.dashboard_block(res), ensure_ascii=False)
    bd = json.dumps(impact.dashboard_board(res), ensure_ascii=False)
    return md + dc + db + bd


# ---------------------------------------------------------------- 1. 過去日の再現
def test_replay():
    out = {}
    for case, want, label in (
        ("20260717_kioxia", lambda it: "キオクシア" in it["headline"] + it["fact"]
         and ({"scandal"} & set(it["categories"])), "キオクシアの米特許訴訟評決"),
        # 11/6 の東証の急落そのもの（3,643円安）を指す記事が、そのアイテムの出典に入っていること。
        # 「半導体」「市況」を含むだけの別の話（例: 同夜の米国株の反発）では合格にしない。
        ("20261106_midterm", lambda it: any(
            any(j in t for j in ("日経平均", "東京株式", "東証", "日本株"))
            and any(k in t for k in ("3643", "３６４３", "全面安", "弱気相場", "史上３番目", "歴代3位"))
            for t in [it["headline"], it["fact"]] + [s["title"] for s in it["sources"]]),
         "11/6 の東証急落（3,643円安・中間選挙後のAI・半導体安）"),
    ):
        if not (FIX / case / "index.json").exists():
            record(f"1. 過去日の再現 {case}", "再現不可", "フィクスチャが無い（make_fixtures.py 未実行）")
            continue
        res = run_case(case)
        out[case] = res
        hits = [i for i, it in enumerate(res["items"], 1) if want(it)]
        top = " | ".join(f"{it['score']}:{it['headline']}" for it in res["items"])
        if hits:
            record(f"1. 過去日の再現 {case}", "合格", f"{label} が {hits[0]}位（上位: {top}）")
        else:
            record(f"1. 過去日の再現 {case}", "不合格",
                   f"{label} が上位5件に無い（上位: {top or 'なし'} / 次点: {res['near_miss']}）")
    return out


# ---------------------------------------------------------------- 2. 失敗系
def test_failure_vs_empty():
    case = "20260717_kioxia"
    now = dt.datetime.fromisoformat(CASES[case]["now"])

    def dead(url, timeout):
        return None, None, "ConnectionError（故障注入）"
    facts = replay_facts(case, now)
    facts["tdnet"] = {"status": "取得失敗: ConnectionError（故障注入）", "items": []}
    facts["news"] = {"status": "全ソース取得失敗", "holdings": [], "sector": [], "macro": [], "errors": [1]}
    failed = impact.build(facts, CFG, now=now, fetcher=dead, sources=replay_sources(), use_llm=False)

    empty_rss = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'

    def empty(url, timeout):
        return 200, empty_rss, None
    facts2 = replay_facts(case, now)
    facts2["tdnet"] = {"status": "直近に開示なし", "items": []}
    facts2["news"] = {"status": "該当なし", "holdings": [], "sector": [], "macro": [], "errors": []}
    facts2["econ_calendar"] = {"status": "ok", "today": [], "upcoming": []}
    emp = impact.build(facts2, CFG, now=now, fetcher=empty, sources=replay_sources(), use_llm=False)

    # 一部だけ落ちる: ok/empty のまま source_errors に残ること
    def half(url, timeout):
        return (None, None, "Timeout（故障注入）") if "kabutan" in url else fixture_fetcher(case)(url, timeout)
    part = impact.build(replay_facts(case, now), CFG, now=now, fetcher=half, sources=replay_sources(),
                        use_llm=False, after=dt.date.fromisoformat(CASES[case]["after"]),
                        before=dt.date.fromisoformat(CASES[case]["before"]))

    f_md, e_md = "\n".join(impact.render_md(failed)), "\n".join(impact.render_md(emp))
    f_dc, e_dc = impact.discord_fields(failed)[0]["value"], impact.discord_fields(emp)[0]["value"]
    f_db, e_db = impact.dashboard_block(failed), impact.dashboard_block(emp)
    none_md = "\n".join(impact.render_md(None))
    ok = (failed["status"] == "failed" and emp["status"] == "empty"
          and "取得できていません" in f_md and "取得できていません" not in e_md
          and "該当なし" in e_md and "該当なし" not in f_md
          and "取得できていません" in f_dc and "該当なし" in e_dc
          and f_db == ([], "failed") and e_db == ([], "empty")
          and "取得できていません" in none_md and impact.dashboard_block(None)[1] == "failed"
          and part["status"] in ("ok", "empty") and any("kabutan" in e["id"] for e in part["source_errors"])
          and "一部ソース取得失敗" in "\n".join(impact.render_md(part)))
    record("2. 失敗系（取得失敗と該当なしの区別）", "合格" if ok else "不合格",
           f"全滅→status={failed['status']} md「{f_md.splitlines()[-1].strip()[:30]}」 / "
           f"0件→status={emp['status']} md「{e_md.splitlines()[-1].strip()[:30]}」 / "
           f"一部失敗→status={part['status']} errors={[e['id'] for e in part['source_errors']]}")


# ---------------------------------------------------------------- 3. LLM無効
def test_no_llm():
    case = "20261106_midterm"
    res = run_case(case, use_llm=False)
    md = "\n".join(impact.render_md(res))
    # LLM を有効にしても接続先が死んでいる場合（ポート9は閉じている）にレポートが止まらないこと
    os.environ["IMPACT_LLM_BASE_URL"], os.environ["IMPACT_LLM_MODEL"] = "http://127.0.0.1:9/v1", "dummy"
    try:
        t0 = time.time()
        res2 = run_case(case, use_llm=True)
        dt_s = time.time() - t0
    finally:
        os.environ.pop("IMPACT_LLM_BASE_URL"); os.environ.pop("IMPACT_LLM_MODEL")
    ok = (res["items"] and all(it["prediction"] is None for it in res["items"])
          and "【予測】なし" in md and PRED_NOTE not in md
          and res2["items"] and all(it["prediction"] is None and str(it["prediction_status"]).startswith("LLM失敗")
                                    for it in res2["items"]))
    record("3. LLM無効で予測欄なし", "合格" if ok else "不合格",
           f"--no-llm: {len(res['items'])}件すべて予測なし / 接続先死亡: "
           f"{[it['prediction_status'] for it in res2['items']][:2]} ({dt_s:.1f}秒で完了)")


PRED_NOTE = impact.PREDICTION_NOTE


# ---------------------------------------------------------------- 4. 予測の数値チェック
def test_number_guard():
    case = "20260717_kioxia"
    base = run_case(case, use_llm=False)
    if not base["items"]:
        record("4. 予測の数値チェック", "不合格", "対象アイテムが0件で検査できない")
        return
    it = base["items"][0]
    real = next((r["chg_pct"] for r in it["market_reaction"]), None)

    def fake_bad(conf, messages, temperature, timeout):
        return json.dumps({"direction": "下落要因", "reason": "株価が12.34%下落した",
                           "risk": "反発すれば外れる", "numbers_used": ["12.34%"]}, ensure_ascii=False)

    def fake_bad_text(conf, messages, temperature, timeout):  # numbers_used は正しいが本文に捏造
        return json.dumps({"direction": "下落要因", "reason": f"{real}%の下落、目標は999円",
                           "risk": "反発", "numbers_used": [f"{real}%"]}, ensure_ascii=False)

    def fake_good(conf, messages, temperature, timeout):
        return json.dumps({"direction": "下落要因", "reason": f"関連銘柄が{real}%動いた",
                           "risk": "控訴で評価が変われば外れる", "numbers_used": [f"{real}%"]}, ensure_ascii=False)

    def fake_banned(conf, messages, temperature, timeout):
        return json.dumps({"direction": "上昇要因", "reason": f"{real}%下落で買い場", "risk": "なし",
                           "numbers_used": [f"{real}%"]}, ensure_ascii=False)
    bad = run_case(case, use_llm=True, llm_caller=fake_bad)
    bad2 = run_case(case, use_llm=True, llm_caller=fake_bad_text)
    good = run_case(case, use_llm=True, llm_caller=fake_good)
    ban = run_case(case, use_llm=True, llm_caller=fake_banned)
    ok = (all(i["prediction"] is None and "入力に無い数値" in i["prediction_status"] for i in bad["items"])
          and all(i["prediction"] is None and "入力に無い数値" in i["prediction_status"] for i in bad2["items"])
          and good["items"][0]["prediction"] is not None
          and all(i["prediction"] is None and "禁止表現" in i["prediction_status"] for i in ban["items"])
          and PRED_NOTE in "\n".join(impact.render_md(good)))
    record("4. 予測の数値チェック", "合格" if ok else "不合格",
           f"捏造数値→「{bad['items'][0]['prediction_status']}」 / 本文に捏造→「{bad2['items'][0]['prediction_status']}」"
           f" / 正しい数値({real}%)→採用 / 禁止語→「{ban['items'][0]['prediction_status']}」")


# ---------------------------------------------------------------- 5. 禁止語
def test_banned(replays: dict):
    texts = {case: all_text(res) for case, res in replays.items()}
    raw_hit = {}
    for case in replays:
        idx = json.loads((FIX / case / "index.json").read_text(encoding="utf-8"))
        raw = b"".join((FIX / case / fn).read_bytes() for fn in idx["urls"].values()).decode("utf-8", "ignore")
        raw_hit[case] = [w for w in impact.BANNED_WORDS if w in raw]
    found = {case: impact.contains_banned(t) for case, t in texts.items()}
    ok = bool(texts) and not any(found.values())
    record("5. 禁止語が出力に無い", "合格" if ok else "不合格",
           f"出力中の禁止語: {found} / 参考: 元の見出し群に含まれていた語 {raw_hit}（言い換え済み）")


# ---------------------------------------------------------------- 6. 処理時間（実ソース）
def test_time_live():
    facts = json.loads(sorted((ROOT / "out").glob("facts_2*.json"))[-1].read_text(encoding="utf-8"))
    t0 = time.time()
    res = impact.build(facts, CFG, use_llm=False)
    el = time.time() - t0
    ok = el <= 90
    record("6. 処理時間（実ソース・impact全体）", "合格" if ok else "不合格",
           f"{el:.1f}秒（上限90秒） status={res['status']} ソース失敗={res['source_errors']}")


if __name__ == "__main__":
    reps = test_replay()
    test_failure_vs_empty()
    test_no_llm()
    test_number_guard()
    test_banned(reps)
    if "--live" in sys.argv:
        test_time_live()
    else:
        record("6. 処理時間", "未実行", "--live を付けて実行すること")
    bad = [r for r in RESULTS if r[1] == "不合格"]
    print(f"\n合格 {sum(r[1] == '合格' for r in RESULTS)} / 不合格 {len(bad)} / "
          f"再現不可 {sum(r[1] == '再現不可' for r in RESULTS)}")
    sys.exit(1 if bad else 0)
