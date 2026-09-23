"""
重要経済指標カレンダー（米国・日本）。

# 取得元の調査結果（2026-09-21 実測）

このリポジトリの開発環境（Claude Code の実行コンテナ）からは、金融データ系の
ホストが egress プロキシで一律に 403（CONNECT tunnel failed）となり、**どの候補も
到達性・応答形式を実測できなかった**。到達できたのは github.com / pypi.org のみ。
「そのソースが使えない」ではなく「この環境からは叩けない」という意味なので混同しないこと。

  候補                                          結果
  --------------------------------------------- ------------------------------------------
  investing.com (investpy 等)                   到達不可(403)。加えて規約・スクレイピング耐性に難
  Trading Economics API                         到達不可(403)。無料枠は指標が限定・要APIキー
  Financial Modeling Prep /economic_calendar    到達不可(403)。要APIキー（無料枠は日数制限）
  Nasdaq economic calendar API                  到達不可(403)
  Yahoo Finance economic calendar               到達不可(403)。公式APIではなく画面依存
  ForexFactory ミラー(nfs.faireconomy.media)    到達不可(403)。非公式ミラーで安定性の保証が無い
  FRED /fred/releases/dates                     到達不可(403)。要APIキー。"リリース日"であり
                                                指標名・予想・前回はそのままでは取れない
  BLS 発表スケジュール(bls.gov)                 到達不可(403)。米雇用統計/CPIの一次情報
  federalreserve.gov FOMCカレンダー             到達不可(403)。FOMC日程の一次情報
  boj.or.jp 金融政策決定会合日程                到達不可(403)。日銀会合日程の一次情報

→ 自動取得元を実測で確定できないため、**タスクの指示どおり config.yaml の手動管理を
  一次ソースとして採用**した。日付を推測で埋めることだけは絶対にしない（earnings.py と同じ方針）。
  実機（ネットワークのあるWindows側）で

      python src/econ.py --probe

  を実行すると上の候補への到達性と応答の先頭を印字する。到達できるものが見つかったら、
  その実測結果をもとに自動取得層をここへ追加すること（未検証のパーサは載せない）。

# 実機での --probe 結果（2026-09-23、デスクトップ DESKTOP-66ECTQP）

上の「到達不可」は開発コンテナの話であって、実機では大半が通る。実測:

  federalreserve.gov FOMCカレンダー   200 text/html
  boj.or.jp 金融政策決定会合日程       200 text/html
  BLS 発表スケジュール                 200 text/html（当月分のページ）
  Nasdaq economic calendar            200 application/json（"asOf":"Wed, Sep 23, 2026"）
  ForexFactory ミラー(非公式)          200 application/json（今週分、国コード付き）
  FRED releases/dates                 400（APIキー未設定。キーがあれば通る見込み）
  Trading Economics (guest)           410（ゲスト廃止。有料APIキーが要る）

  → 自動取得層は「作れる」状態になった。ただし応答を1回見ただけでパーサを載せない。
    実装するときは (1) 祝日・臨時会合を含む複数週で形が変わらないか、(2) 予想値・時刻が
    欠ける日をどう出すか（推測で埋めない）、(3) 手動カレンダーとどちらを優先するか
    を決めてから。それまでは config.yaml の手動管理のまま。

# 手動カレンダーの安全装置

「本日は予定なし」と「カレンダーの更新が止まっている」は全く違う。後者を前者に
見せかけないため、登録済みイベントの最終日が今日より前なら status を「要更新」にする。
"""
from __future__ import annotations

import datetime as dt
import re
import sys

JST = dt.timezone(dt.timedelta(hours=9))

IMPORTANCE = {"high": "★★★", "medium": "★★", "low": "★"}
COUNTRY_LABEL = {"US": "米", "JP": "日", "EU": "欧", "CN": "中", "GB": "英"}

DEFAULT_UPCOMING_DAYS = 7

# --probe が叩く候補。URLはコメントではなくコードに持たせる（実機で即実行できるように）。
PROBE_TARGETS = [
    ("federalreserve.gov FOMCカレンダー", "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"),
    ("boj.or.jp 金融政策決定会合日程", "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"),
    ("BLS 発表スケジュール", "https://www.bls.gov/schedule/news_release/"),
    ("FRED releases/dates (要APIキー)", "https://api.stlouisfed.org/fred/releases/dates?file_type=json"),
    ("Nasdaq economic calendar", "https://api.nasdaq.com/api/calendar/economicevents"),
    ("ForexFactory ミラー(非公式)", "https://nfs.faireconomy.media/ff_calendar_thisweek.json"),
    ("Trading Economics (要APIキー)", "https://api.tradingeconomics.com/calendar?c=guest:guest"),
]


def _norm_time(v) -> tuple[str | None, str | None]:
    """'21:30' → ('21:30', None) / 未定・不明 → (None, 理由)。推測で時刻を作らない。"""
    if v in (None, "", "—", "未定"):
        return None, None
    s = str(v).strip()
    if re.match(r"^\d{1,2}:\d{2}$", s):
        h, m = (int(x) for x in s.split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}", None
    if s in ("終日", "未定時刻"):
        return None, None
    return None, f"time_jst の書式が不正: {v!r}（HH:MM または 未記入のみ）"


def _validate(ev: dict) -> tuple[dict | None, str | None]:
    """1件を検証して正規化する。1つでも欠ければ採用しない（穴埋めしない）。"""
    date = str(ev.get("date") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return None, f"date が YYYY-MM-DD でない: {ev.get('date')!r}"
    try:
        dt.date.fromisoformat(date)
    except ValueError:
        return None, f"date が実在しない日付: {date}"
    name = str(ev.get("name") or "").strip()
    if not name:
        return None, f"{date}: name が空"
    country = str(ev.get("country") or "").strip().upper()
    if not country:
        return None, f"{date} {name}: country が空"
    imp = str(ev.get("importance") or "").strip().lower()
    if imp not in IMPORTANCE:
        return None, f"{date} {name}: importance が {sorted(IMPORTANCE)} のいずれでもない: {ev.get('importance')!r}"
    time_jst, terr = _norm_time(ev.get("time_jst"))
    if terr:
        return None, f"{date} {name}: {terr}"
    return {
        "date": date,
        "time_jst": time_jst,                    # None = 時刻未定（表示側は「—」）
        "country": country,
        "country_label": COUNTRY_LABEL.get(country, country),
        "name": name,
        "importance": imp,
        "importance_label": IMPORTANCE[imp],
        # 予想・前回は取れた時だけ。取れなければ None のまま（表示側が「—」にする）
        "forecast": ev.get("forecast") if ev.get("forecast") not in ("", None) else None,
        "previous": ev.get("previous") if ev.get("previous") not in ("", None) else None,
        "source": ev.get("source") or "manual (config.yaml)",
        "source_url": ev.get("source_url"),
        "confirmed": True,   # 人が一次情報で確認して書いた前提。推定日は書かない運用
        "note": ev.get("note"),
    }, None


def _sort_key(e: dict) -> tuple:
    return (e["date"], e["time_jst"] or "99:99", e["name"])


def build(cfg: dict, today: dt.date | None = None) -> dict:
    """config.yaml の econ_calendar から、本日分と今後N日分を組み立てる。"""
    today = today or dt.datetime.now(JST).date()
    out: dict = {
        "generated_at_jst": dt.datetime.now(JST).isoformat(),
        "date": today.isoformat(),
        "today": [],
        "upcoming": [],
        "invalid": [],
        "gaps": [],
        "sources": ["config.yaml econ_calendar（人が公式サイトで確認して記入した日程）"],
        "method": "手動管理。自動取得元は未採用（econ.py のモジュールdocstringに調査結果）。",
    }
    try:
        spec = (cfg or {}).get("econ_calendar") or {}
        raw = spec.get("events") or []
        days = int(spec.get("upcoming_days") or DEFAULT_UPCOMING_DAYS)
        # 登録できていない期間の自己申告。events の途中に空いた穴は coverage_until では
        # 捕まらず、その日が「本日は予定なし」と表示されてしまう。穴は人が書いて常時併記する。
        gaps = [str(g) for g in (spec.get("gaps") or []) if str(g).strip()]
    except Exception as e:
        return {**out, "status": f"取得失敗: {type(e).__name__}: {e}"}

    events = []
    for ev in raw:
        norm, err = _validate(ev if isinstance(ev, dict) else {})
        if err:
            out["invalid"].append(err)       # 黙って捨てない
        else:
            events.append(norm)
    events.sort(key=_sort_key)

    out["gaps"] = gaps
    out["today"] = [e for e in events if e["date"] == today.isoformat()]
    horizon = (today + dt.timedelta(days=days)).isoformat()
    out["upcoming"] = [e for e in events
                       if today.isoformat() < e["date"] <= horizon]
    out["upcoming_days"] = days
    out["n_registered"] = len(events)
    out["coverage_until"] = events[-1]["date"] if events else None
    out["high_today"] = [e for e in out["today"] if e["importance"] == "high"]

    if not raw:
        out["status"] = ("未設定（config.yaml の econ_calendar.events が空）。"
                         "取得失敗ではなく、まだ1件も登録されていない状態。")
    elif not events:
        out["status"] = (f"登録{len(raw)}件はすべて書式不正で採用できなかった"
                         f"（詳細は invalid）。該当ゼロではない。")
    elif out["coverage_until"] < today.isoformat():
        # ここが一番危ない状態。「今日は予定なし」と区別できるように必ず別statusにする。
        out["status"] = (f"要更新（登録済みの最終日が {out['coverage_until']} で、"
                         f"本日({today})以降の予定が1件も無い）。"
                         "『本日は予定なし』ではなく、カレンダーの更新が止まっている。")
    else:
        out["status"] = "ok"
    return out


# ---------------------------------------------------------------- 実機での到達性調査
def probe() -> int:
    """候補ソースへの到達性と応答の先頭を印字する。ネットワークのある実機で実行する。"""
    import requests
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) morning-report/1.0"
    ng = 0
    for label, url in PROBE_TARGETS:
        try:
            r = requests.get(url, headers={"User-Agent": ua}, timeout=20)
            ct = r.headers.get("Content-Type", "?")
            head = re.sub(r"\s+", " ", r.text[:160])
            print(f"[{r.status_code}] {label}\n    {url}\n    Content-Type: {ct}\n    先頭: {head}\n")
            if r.status_code >= 400:
                ng += 1
        except Exception as e:
            ng += 1
            print(f"[NG ] {label}\n    {url}\n    {type(e).__name__}: {e}\n")
    print(f"到達不可: {ng}/{len(PROBE_TARGETS)} 件")
    return ng


if __name__ == "__main__":
    if "--probe" in sys.argv:
        probe()
    else:
        import json
        import pathlib
        import yaml
        root = pathlib.Path(__file__).resolve().parents[1]
        cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        print(json.dumps(build(cfg), ensure_ascii=False, indent=1))
