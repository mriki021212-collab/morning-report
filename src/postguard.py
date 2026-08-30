"""投稿の重複と、時刻がずれた投稿を止める。

## なぜ要るか

このシステムは同じ `src/main.py` を2つのスケジューラから走らせている
（デスクトップのタスクスケジューラと GitHub Actions）。両者は互いを知らず、
main.py にも投稿済み判定が無かったため、実測で次のことが起きていた。

- 2026-08-28: デスクトップが 08:30 JST に朝レポートを投稿し、同じ日の
  GitHub Actions morning が **16:00 JST** に走って同じ朝レポートをもう一度投稿した。
- 2026-08-28 の afternoon ワークフローは 3本とも **8/29(土) 04:10-04:34 JST** に発火した。
  JST日付が土曜に転がるので main.py は休場ブランチに入り、土曜の未明に
  「東証休場」通知が飛んだ。

原因は2つある。(1) 2つのスケジューラで投稿済み状態を共有していない。
(2) GitHubのscheduleは「遅延」が数分で済む保証がなく、実測で5〜12時間ずれた。

## 設計

**1日1セッションにつき通知は1通**、という規則をこのモジュールで一元化する。

- マーカーは `out/posted.json`。**gitにコミットして共有する。**
  デスクトップとActionsが共有できるチャンネルはリポジトリしかない。
  デスクトップは実行前に `git reset --hard origin/main` し、Actionsは毎回
  checkout するので、どちらも直前の状態を読める。
- 想定時間帯を外れた実行は**レポートを投稿しない**。寄り付き前レポートを
  大引け後に配れば、内容は静かに嘘になる。ただし黙って終わらない
  （無音 = 異常、という前提を壊さないため）。1行の遅延通知だけ出す。

## 残っている穴（承知の上）

デスクトップとActionsが数秒差で同時に起動した場合、両方が「未投稿」を読んで
両方が投稿しうる。実測ではActionsが数時間ずれているので現実的な窓は狭いが、
ゼロではない。完全に消すには単一の排他機構が要る。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

JST = dt.timezone(dt.timedelta(hours=9))

# セッションごとの「投稿してよい時間帯」(JST)。
# morning : 寄り付き(09:00)前のレポート。定時は08:30。09:30を過ぎたら前提が崩れる。
# afternoon: 大引け(15:30)後の振り返り。定時は16:00、データ確定待ちで16:40まで再試行。
#            夜のうちは意味を保つので22:00まで許す。
WINDOWS = {
    "morning": (dt.time(6, 0), dt.time(9, 30)),
    "afternoon": (dt.time(15, 40), dt.time(22, 0)),
}

MARKER = "posted.json"


def marker_path(out_dir: pathlib.Path) -> pathlib.Path:
    return out_dir / MARKER


def _load(out_dir: pathlib.Path) -> dict:
    p = marker_path(out_dir)
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        # 壊れたマーカーは「未投稿」として扱う。
        # ここで例外を上げるとレポートが出なくなる = 沈黙する。
        # 二重投稿より沈黙のほうが悪い、という優先順位でこう倒す。
        return {}


def already_notified(out_dir: pathlib.Path, session: str, today: dt.date) -> str | None:
    """今日そのセッションで既に何か通知したなら、その種別を返す。無ければ None。"""
    rec = _load(out_dir).get(session)
    if isinstance(rec, dict) and rec.get("date") == today.isoformat():
        return rec.get("kind") or "unknown"
    return None


def record(out_dir: pathlib.Path, session: str, today: dt.date, kind: str) -> None:
    """通知したことを記録する。kind は report / holiday / late のいずれか。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    d = _load(out_dir)
    d[session] = {
        "date": today.isoformat(),
        "kind": kind,
        "at": dt.datetime.now(JST).isoformat(timespec="seconds"),
    }
    p = marker_path(out_dir)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)  # 読み取り中の中途半端なJSONを避ける


def in_window(session: str, now: dt.datetime | None = None) -> bool:
    now = now or dt.datetime.now(JST)
    lo, hi = WINDOWS[session]
    return lo <= now.time() <= hi


def window_text(session: str) -> str:
    lo, hi = WINDOWS[session]
    return f"{lo:%H:%M}-{hi:%H:%M} JST"
