from __future__ import annotations
import datetime as dt
import io
import os
import json
import requests

JST = dt.timezone(dt.timedelta(hours=9))
LIMIT = 1900

GREEN = 0x2ECC71
RED = 0xE74C3C
GRAY = 0x95A5A6
ORANGE = 0xE67E22
PURPLE = 0x8E44AD  # ファクトチェック不一致専用。地合いの赤/緑と意味が違うので別色にする

DASHBOARD_URL = "https://mriki021212-collab.github.io/morning-report/terminal_dashboard.html"
MOVER_ALERT_PCT = 3.0  # 保有株の前日比がこれ以上なら結論行で警告する


def _chunks(text: str) -> list[str]:
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > LIMIT:
            out.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        out.append(buf)
    return out


def _pct(v):
    if v is None:
        return "取得不可"
    arrow = "🔺" if v > 0 else "🔻" if v < 0 else "➖"
    return f"{arrow}{abs(v):.2f}%"


def _mood_avg(facts: dict) -> float | None:
    macro = facts.get("macro", {}) if facts else {}
    vals = []
    for key in ("^N225", "^SOX"):
        s = macro.get(key) or {}
        if not s.get("status") and isinstance(s.get("chg_pct"), (int, float)):
            vals.append(s["chg_pct"])
    return sum(vals) / len(vals) if vals else None


def _mood_color(facts: dict) -> int:
    avg = _mood_avg(facts)
    if avg is None:
        return GRAY
    return GREEN if avg > 0 else RED if avg < 0 else GRAY


def _biggest_mover(facts: dict):
    """保有銘柄のうち前日比の絶対値が最大のものを (name, chg_pct) で返す。無ければ None"""
    best = None
    for s in (facts.get("holdings", {}) or {}).values():
        chg = s.get("chg_pct")
        if s.get("status") or not isinstance(chg, (int, float)):
            continue
        if best is None or abs(chg) > abs(best[1]):
            best = (s.get("name", "?"), chg)
    return best


def _is_audit_clean(verdict: str) -> bool:
    v = (verdict or "").strip()
    return v == "OK" or "未使用" in v  # 「数値のみ（LLM未使用）」は不一致ではない


def _conclusion(facts: dict, audit_result: str) -> str:
    """embed descriptionに載せる結論。優先度: 要注意開示 → 地合い → 急変銘柄 → 監査警告"""
    lines = []
    hs = (facts.get("tdnet", {}) or {}).get("high_signal", []) if facts else []
    if hs:
        i = hs[0]
        extra = f" ほか{len(hs) - 1}件" if len(hs) > 1 else ""
        lines.append(f"🚨 **{i['company']}**: {i['title']}{extra}")

    avg = _mood_avg(facts) if facts else None
    if avg is None:
        lines.append("⚪ **地合い: データ不足**")
    elif avg > 0:
        lines.append(f"🟢 **地合い: 上昇**（日経+SOX平均 {avg:+.2f}%）")
    elif avg < 0:
        lines.append(f"🔴 **地合い: 下落**（日経+SOX平均 {avg:+.2f}%）")
    else:
        lines.append("⚪ **地合い: 変わらず**")

    mover = _biggest_mover(facts) if facts else None
    if mover and abs(mover[1]) >= MOVER_ALERT_PCT:
        name, chg = mover
        arrow = "🔺" if chg > 0 else "🔻"
        lines.append(f"🚨 **{name}** が{arrow}{abs(chg):.2f}%の大幅変動")

    if not _is_audit_clean(audit_result):
        lines.append("⚠️ **ファクトチェックで数値の不一致が検出されました**（詳細は下部参照）")

    return "\n".join(lines)


def _macro_field(facts: dict) -> dict:
    macro = facts.get("macro", {}) if facts else {}
    lines = []
    for code, s in macro.items():
        if s.get("status"):
            lines.append(f"{code:<10} 取得不可")
            continue
        name = s.get("name", code)
        lines.append(f"{name:<10} {s['close']:>12,.2f}  {_pct(s.get('chg_pct'))}")
    body = "\n".join(lines) if lines else "データなし"
    return {"name": "📊 米国市場・マクロ", "value": f"```\n{body}\n```", "inline": False}


def _gap_field(facts: dict) -> dict:
    g = facts.get("nikkei_gap", {}) if facts else {}
    if not g or g.get("status"):
        return {"name": "📈 日経寄り付き示唆", "value": "現時点では確認できない", "inline": True}
    if g.get("valid") is False:
        return {"name": "📈 日経寄り付き示唆", "value": f"使用不可 — {g.get('warning','')}", "inline": True}
    pts = g.get("implied_gap_pts")
    pct = g.get("implied_gap_pct")
    if pts is None or pct is None:
        return {"name": "📈 日経寄り付き示唆", "value": "算出不可", "inline": True}
    arrow = "🔺GU" if pts > 0 else "🔻GD" if pts < 0 else "➖"
    return {"name": "📈 日経寄り付き示唆",
            "value": f"```\n{arrow}  {pts:+,.0f}円 ({pct:+.2f}%)\n```", "inline": True}


def _alert_field(high_signal: list[dict]) -> dict:
    lines = []
    for i in high_signal[:5]:
        lines.append(f"**[{i['company']}]** {i['title']}  "
                     f"（{i['time']} / {'・'.join(i['high_signal_words'])}）  [PDF]({i['url']})")
    return {"name": "🚨 要注意開示（TDnet）", "value": "\n".join(lines)[:1024], "inline": False}


def _factcheck_field(audit_result: str) -> dict:
    ok = _is_audit_clean(audit_result)
    value = "✅ 不一致なし" if ok else f"⚠️ 不一致あり\n{audit_result[:150]}"
    return {"name": "🔍 ファクトチェック", "value": value, "inline": True}


def _dashboard_field() -> dict:
    return {"name": "🔗 ライブダッシュボード",
            "value": f"[ターミナル表示を開く]({DASHBOARD_URL})", "inline": False}


def _holdings_field(facts: dict) -> dict:
    h = facts.get("holdings", {}) if facts else {}
    lines = []
    for code, s in h.items():
        if s.get("status"):
            lines.append(f"{code:<8} 取得不可")
            continue
        name = s.get("name", code)
        close = s.get("close")
        chg = s.get("chg_pct")
        close_str = f"{close:>10,.1f}" if isinstance(close, (int, float)) else "     ―"
        lines.append(f"{name:<12}{close_str}  {_pct(chg)}")
    body = "\n".join(lines) if lines else "データなし"
    return {"name": "💼 保有銘柄", "value": f"```\n{body}\n```", "inline": False}


def _session_close_field(facts: dict) -> dict | None:
    """後場版のみ: 当日終値が未確定なら、それを埋もれさせず最上段に出す。
    ここが未確定のまま届いたレポートは「本日の振り返り」として読んではいけない。"""
    sc = facts.get("session_close") or {}
    if not sc or sc.get("confirmed"):
        return None
    names = "・".join((p.get("name") or p["code"]) for p in sc.get("pending", [])[:6])
    return {"name": "⚠️ 当日終値が未確定",
            "value": f"{names} の日足がまだ {sc.get('expected_date')} 分に更新されていません。"
                     "本日の値動きとしては読めません（後続cronの再取得待ち）。",
            "inline": False}


def post(report: str, audit_result: str = "OK", facts: dict | None = None) -> None:
    url = os.environ["DISCORD_WEBHOOK_URL"]
    now = dt.datetime.now(JST)
    afternoon = bool(facts) and facts.get("session") == "afternoon"
    stale_close = afternoon and not (facts.get("session_close") or {}).get("confirmed")

    # 色の優先度: 当日終値未確定(橙) > 監査不一致(紫) > 保有株の要注意開示(橙) > 地合い(緑/赤/灰)
    # 未確定を最優先にするのは、地合いの緑で「正常に見える」のが一番危ないため。
    if stale_close:
        color = ORANGE
    elif not _is_audit_clean(audit_result):
        color = PURPLE
    elif facts and (facts.get("tdnet", {}) or {}).get("high_signal"):
        color = ORANGE
    else:
        color = _mood_color(facts) if facts else GRAY

    fields = []
    if facts:
        sf = _session_close_field(facts) if afternoon else None
        if sf:
            fields.append(sf)
        hs = (facts.get("tdnet", {}) or {}).get("high_signal", [])
        if hs:
            fields.append(_alert_field(hs))
        fields.append(_holdings_field(facts))
        # 寄り付き示唆は朝だけ意味を持つ。大引け後に出しても読み手を混乱させるだけ。
        if not afternoon:
            fields.append(_gap_field(facts))
        fields.append(_factcheck_field(audit_result))
        fields.append(_macro_field(facts))
    fields.append(_dashboard_field())

    prefix = "afternoon" if afternoon else "morning"
    files = {"file": (f"{prefix}_{now:%Y%m%d}.md",
                      io.BytesIO(report.encode("utf-8")), "text/markdown")}
    title = (f"🌇 アフタヌーンレポート {now:%Y/%m/%d (%a) %H:%M} JST"
             if afternoon else f"🗾 モーニングレポート {now:%Y/%m/%d (%a) %H:%M} JST")
    payload = {
        "username": "Morning Strategist",
        "embeds": [{
            "title": title,
            "url": DASHBOARD_URL,
            "description": _conclusion(facts, audit_result) if facts else "⚪ **データ取得なし**",
            "color": color,
            "fields": fields,
            "footer": {"text": "自動生成レポート・投資助言ではありません"},
        }],
    }
    r = requests.post(url, data={"payload_json": json.dumps(payload)},
                      files=files, timeout=30)
    r.raise_for_status()

    for c in _chunks(report):
        requests.post(url, json={"content": f"```\n{c}\n```"[:1990]}, timeout=30).raise_for_status()


def post_holiday(d, session: str = "morning") -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    kind = "アフタヌーンレポート" if session == "afternoon" else "モーニングレポート"
    requests.post(url, json={
        "embeds": [{
            "title": f"{d:%Y/%m/%d (%a)} ― 東証休場",
            "description": f"本日は{kind}なし。システムは正常に稼働しています。",
            "color": GRAY,
        }]}, timeout=30)


def post_error(msg: str) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    requests.post(url, json={"content": f"⚠ レポート生成失敗\n```\n{msg[:1800]}\n```"}, timeout=30)

def post_late_skip(session: str, now, window: str) -> None:
    """定時から大きくずれて起動した回の通知。レポート本体の代わりに1行だけ出す。

    寄り付き前レポートを大引け後に配ると、内容はエラーを出さないまま嘘になる。
    かといって黙って終わると「休場」「cron不発」「クラッシュ」がまた同じ無音に
    戻ってしまう。だから本体は出さず、ずれたという事実だけを出す。

    実測の背景: GitHubのscheduleが 2026-08-27 以降 5〜12時間ずれて発火し、
    朝レポートが 16:00 JST に、後場レポートが翌土曜 04:10 JST に走っていた。
    """
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    kind = "アフタヌーンレポート" if session == "afternoon" else "モーニングレポート"
    requests.post(url, json={
        "embeds": [{
            "title": f"{kind} ― 定時に発火せず",
            "description": (
                f"この実行は **{now:%Y/%m/%d %H:%M} JST**。想定時間帯は {window}。\n"
                f"時間帯を外れているため、レポート本体は投稿していません"
                f"（古い前提のまま配ると内容が静かに嘘になるため）。\n"
                f"スケジューラ側の遅延を確認してください。"
            ),
            "color": ORANGE,
        }]}, timeout=30)
