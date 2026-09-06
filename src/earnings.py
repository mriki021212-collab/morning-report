"""
決算発表予定日を集めて out/earnings.json を書き出す。

このモジュールの最重要ルール: 日付を推測しない。
「前年同期が8月上旬だったから今年も同じはず」という補完は一切しない。
取れなければ events に入れず failed に積む。空の日付では埋めない。

ソースの優先順位 (先勝ち):
  1. manual   … config.yaml に人がIRページで確認して書いた日付。confirmed=true
  2. jquants  … 未検証。トークンがある環境でだけ動く追加ソース (下の注記参照)
  3. yfinance … isEarningsDateEstimate で確定/推定を機械判定できる

yfinance を使う上で実測で判明した罠が3つある。いずれも回避済み:
  - .calendar の "Earnings Date" は「実行マシンのローカルTZ」で日付化される。
    JSTマシンとUTCマシンで日付が1日ずれる。よって .calendar は使わず、
    生の earningsTimestampStart/End を明示的にJST変換する。
  - earningsTimestamp は「前回の決算」を指すことがある (8035.Tで実測)。
    将来日が入るのは earningsTimestampStart/End のほうなので、そちらだけ使う。
  - 過去日が残ったままの銘柄がある (6740.Tで 1年前の日付を返した)。
    当日より前の日付は捨てて failed に積む。

日付は JST の暦日で持つ。米国企業は現地16:00発表 = JST翌朝5:00 で、
東京市場が反応するのはそのJST日付の寄り付きだから、この画面ではJSTが正しい枠組み。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys

import yaml

JST = dt.timezone(dt.timedelta(hours=9))
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _jst_date(ts: int | float | None) -> dt.date | None:
    if not ts:
        return None
    return dt.datetime.fromtimestamp(ts, tz=JST).date()


def _from_manual(targets: list[dict], manual: dict) -> tuple[list[dict], set[str]]:
    """config.yaml に人が書いた日付。一次情報で確認済みという前提なので confirmed=true。"""
    events, got = [], set()
    by_code = {t["code"]: t for t in targets}
    for code, spec in (manual or {}).items():
        date = (spec or {}).get("date")
        if not date:
            continue
        t = by_code.get(code, {})
        events.append({
            "code": code,
            "name": t.get("name", code),
            "date": str(date),
            "confirmed": True,
            "source": "manual",
            "period": spec.get("period"),
            "region": t.get("region", "JP"),
            "source_url": spec.get("source_url"),
        })
        got.add(code)
    return events, got


def _from_jquants(codes: list[str]) -> tuple[list[dict], list[str]]:
    """J-Quants の決算発表予定日。

    注意: 未検証。このリポジトリの開発機に JQ_REFRESH_TOKEN が無く、応答形式を実測できていない。
    推測でパースして「それらしい嘘」を返すことだけは避けたいので、
    期待するキーが無ければ黙って諦める (捏造しない・例外も投げない)。
    トークンのある環境で実測できたら、ここを実データに合わせて確定させること。
    yfinance + manual だけで画面は成立するので、これは後から挿さる追加ソースの位置づけ。
    """
    if not os.getenv("JQ_REFRESH_TOKEN"):
        return [], []
    try:
        import jquants
        token = jquants._id_token()
        if not token:
            return [], list(codes)
        data = jquants._get("/fins/announcement", token)
        rows = data.get("announcement")
        if not isinstance(rows, list):
            print("[earnings] J-Quants: 応答形式が想定と違うため使用しない", file=sys.stderr)
            return [], []
        wanted = {c.replace(".T", ""): c for c in codes}
        events = []
        for r in rows:
            c4 = str(r.get("Code", ""))[:4]
            date = r.get("Date")
            if c4 in wanted and date:
                events.append({
                    "code": wanted[c4], "date": str(date), "confirmed": True,
                    "source": "jquants", "period": r.get("TypeOfDocument"),
                })
        return events, []
    except Exception as e:
        print(f"[earnings] J-Quants 取得失敗: {e}", file=sys.stderr)
        return [], list(codes)


def _from_yfinance(targets: list[dict], today: dt.date) -> tuple[list[dict], list[str]]:
    import yfinance as yf

    events, failed = [], []
    for t in targets:
        code = t["code"]
        try:
            info = yf.Ticker(code).get_info()
        except Exception as e:
            print(f"[earnings] {code}: yfinance 取得失敗: {e}", file=sys.stderr)
            failed.append(code)
            continue

        start = _jst_date(info.get("earningsTimestampStart"))
        end = _jst_date(info.get("earningsTimestampEnd"))
        if start is None or start < today:
            # 将来日が無い / 過去日が残っているだけ。推測で埋めず素直に落とす。
            failed.append(code)
            continue

        # start != end は「この期間のどこか」という幅のある予定。確定日ではありえない。
        estimate = bool(info.get("isEarningsDateEstimate")) or (end is not None and end != start)
        events.append({
            "code": code,
            "name": t.get("name", code),
            "date": start.isoformat(),
            "confirmed": not estimate,
            "source": "yfinance",
            # 期(1Q等)は yfinance から取れない。会計年度末から逆算するのは推測なので null のままにする。
            "period": None,
            "region": t.get("region", "JP"),
            "window_end": end.isoformat() if end and end != start else None,
        })
    return events, failed


def build(cfg: dict, today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now(JST).date()
    ecfg = cfg.get("earnings") or {}
    targets = ecfg.get("targets") or []
    held = {h["code"] for h in cfg.get("holdings", [])}

    events, got = _from_manual(targets, ecfg.get("manual"))
    sources = ["manual"] if events else []

    remaining = [t for t in targets if t["code"] not in got]

    jq_events, _ = _from_jquants([t["code"] for t in remaining])
    by_code = {t["code"]: t for t in remaining}
    for e in jq_events:
        if e["code"] in got:
            continue
        t = by_code.get(e["code"], {})
        e.setdefault("name", t.get("name", e["code"]))
        e.setdefault("region", t.get("region", "JP"))
        events.append(e)
        got.add(e["code"])
    if jq_events:
        sources.append("jquants")

    remaining = [t for t in targets if t["code"] not in got]
    yf_events, _ = _from_yfinance(remaining, today)
    events.extend(yf_events)
    if yf_events:
        sources.append("yfinance")

    # 過去日は出さない。manual に古い日付が残っていた場合もここで落ちる。
    events = [e for e in events if e["date"] >= today.isoformat()]
    for e in events:
        e["held"] = e["code"] in held
    events.sort(key=lambda e: (e["date"], e["code"]))

    # events に載らなかった対象は全部 failed。個別の失敗理由を積み上げるより、
    # 「対象なのに予定が出せなかった銘柄」を最後に差分で取るほうが取りこぼさない。
    have = {e["code"] for e in events}
    failed = sorted(t["code"] for t in targets if t["code"] not in have)
    return {
        "as_of": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "sources": sources,
        "events": events,
        "failed": failed,
        # 当日決算をアラートに上げる国内主力。HTML側でハードコードせずここを見る
        "bellwethers": ecfg.get("sector_bellwethers") or [],
    }


# ---------------------------------------------------------------------------
# 過去の決算発表日
#
# 上の build() は「これから先の予定」しか持たない (過去日は意図的に捨てている)。
# accumulation.py は逆に「過去のスパイク日が決算の日だったか」を知りたいので、
# 過去日の一次ソースが別に要る。
#
# yfinance の get_earnings_dates() は使えない。2026-09-06 に実測したところ、
#   7974.T max=2025-05-08 / 8035.T max=2025-04-30 / 6920.T max=2025-04-28
#   285A.T max=2025-05-15 / NVDA max=2025-06-26
# と、どの銘柄も約16か月前で止まっていた。株価は 2026-09-04 まで取れているので、
# 直近1年ぶんのスパイクは全て「決算日不明」になってしまう。
#
# TDnet (やのしんWEB-API) は決算短信そのものの開示日時を返す。同日に実測:
#   7974 は 2009-10-29 から 2026-08-06 まで68件、285A は上場後の8件、
#   5803 は 2026-08-07 14:00 (＝大引け前の開示) まで取れた。
# 一次情報でありタイムスタンプまで持つので、こちらを使う。
# ---------------------------------------------------------------------------
TDNET_LIST = "https://webapi.yanoshin.jp/webapi/tdnet/list"
_KESSAN = "決算短信"


def _parse_pubdate(raw: str) -> dt.datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            when = dt.datetime.strptime((raw or "").strip(), fmt)
            return when if when.tzinfo else when.replace(tzinfo=JST)
        except (ValueError, AttributeError):
            continue
    return None


def _fetch_past_one(code: str, limit: int) -> dict:
    """1銘柄ぶんの過去決算短信。失敗は必ず status に出す（空リストで隠さない）。"""
    import requests

    url = f"{TDNET_LIST}/{code.replace('.T', '')}.json?limit={limit}"
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": "morning-report/1.0"})
        r.raise_for_status()
        rows = r.json().get("items", [])
    except Exception as e:
        return {"status": f"取得失敗: {type(e).__name__}: {e}",
                "announcements": None, "covers_from": None}

    anns, all_dates = [], []
    for row in rows:
        d = row.get("Tdnet", row)
        when = _parse_pubdate(d.get("pubdate") or d.get("update") or "")
        if when is None:
            continue
        all_dates.append(when)
        if _KESSAN in (d.get("title") or ""):
            anns.append({"datetime_jst": when.isoformat(timespec="seconds"),
                         "title": d.get("title", "")})
    anns.sort(key=lambda a: a["datetime_jst"])
    return {
        "status": "ok" if anns else "決算短信の開示が見つからない",
        # このAPIは新しい順に limit 件を返す。limit で頭打ちになった銘柄は、
        # ここより古いスパイクを「決算ではない」と言い切れない。その境界を必ず持ち回る。
        "covers_from": min(all_dates).date().isoformat() if all_dates else None,
        "truncated": len(rows) >= limit,
        "announcements": anns,
    }


def past_announcements(codes: list[str], out_dir: pathlib.Path | None = None,
                       cache_hours: int = 168, limit: int = 300) -> dict:
    """過去の決算発表日時を銘柄ごとに返す。

    返り値: {code: {"status": ..., "covers_from": "YYYY-MM-DD"|None,
                    "announcements": [{"datetime_jst": ...}, ...] | None}}
    announcements が None なら「取れなかった」。空リストなら「開示が無かった」。
    この2つを呼び出し側が区別できるようにしておくこと。

    過去日は後から変わらないのでキャッシュしてよい。キャッシュは
    out/earnings_past.json。取得失敗した銘柄はキャッシュに残さない
    （失敗を成功として固定してしまうため）。
    """
    cache: dict = {}
    path = out_dir / "earnings_past.json" if out_dir else None
    now = dt.datetime.now(JST)
    if path and path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    out, fetched = {}, False
    for code in codes:
        c = cache.get(code)
        if c and c.get("fetched_at"):
            age = (now - dt.datetime.fromisoformat(c["fetched_at"])).total_seconds() / 3600
            if age < cache_hours and c.get("announcements") is not None:
                out[code] = {k: c[k] for k in ("status", "covers_from", "truncated",
                                               "announcements") if k in c}
                continue
        res = _fetch_past_one(code, limit)
        out[code] = res
        fetched = True
        if res.get("announcements") is not None:
            cache[code] = dict(res, fetched_at=now.isoformat(timespec="seconds"))

    if path and fetched:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def write(cfg: dict, out_dir: pathlib.Path) -> dict:
    data = build(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "earnings.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


if __name__ == "__main__":
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    data = write(cfg, ROOT / "out")
    print(json.dumps(data, ensure_ascii=False, indent=1))
