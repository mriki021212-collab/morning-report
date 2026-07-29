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


def _mood_color(facts: dict) -> int:
    macro = facts.get("macro", {}) if facts else {}
    vals = []
    for key in ("^N225", "^SOX"):
        s = macro.get(key) or {}
        if not s.get("status") and isinstance(s.get("chg_pct"), (int, float)):
            vals.append(s["chg_pct"])
    if not vals:
        return GRAY
    avg = sum(vals) / len(vals)
    return GREEN if avg > 0 else RED if avg < 0 else GRAY


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
        return {"name": "📈 日経寄り付き示唆", "value": "現時点では確認できない", "inline": False}
    if g.get("valid") is False:
        return {"name": "📈 日経寄り付き示唆", "value": f"使用不可 — {g.get('warning','')}", "inline": False}
    pts = g.get("implied_gap_pts")
    pct = g.get("implied_gap_pct")
    if pts is None or pct is None:
        return {"name": "📈 日経寄り付き示唆", "value": "算出不可", "inline": False}
    arrow = "🔺GU" if pts > 0 else "🔻GD" if pts < 0 else "➖"
    return {"name": "📈 日経寄り付き示唆",
            "value": f"```\n{arrow}  {pts:+,.0f}円 ({pct:+.2f}%)\n```", "inline": False}


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


def post(report: str, audit_result: str = "OK", facts: dict | None = None) -> None:
    url = os.environ["DISCORD_WEBHOOK_URL"]
    now = dt.datetime.now(JST)
    color = _mood_color(facts) if facts else (0x2E86C1 if audit_result == "OK" else ORANGE)

    fields = []
    if facts:
        fields = [_macro_field(facts), _gap_field(facts), _holdings_field(facts)]

    files = {"file": (f"morning_{now:%Y%m%d}.md",
                      io.BytesIO(report.encode("utf-8")), "text/markdown")}
    payload = {
        "username": "Morning Strategist",
        "embeds": [{
            "title": f"🗾 モーニングレポート {now:%Y/%m/%d (%a) %H:%M} JST",
            "color": color,
            "fields": fields,
            "footer": {"text": f"ファクトチェック: {audit_result[:200]}"},
        }],
    }
    r = requests.post(url, data={"payload_json": json.dumps(payload)},
                      files=files, timeout=30)
    r.raise_for_status()

    for c in _chunks(report):
        requests.post(url, json={"content": f"```\n{c}\n```"[:1990]}, timeout=30).raise_for_status()


def post_holiday(d) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    requests.post(url, json={
        "embeds": [{
            "title": f"{d:%Y/%m/%d (%a)} ― 東証休場",
            "description": "本日はレポートなし。システムは正常に稼働しています。",
            "color": GRAY,
        }]}, timeout=30)


def post_error(msg: str) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    requests.post(url, json={"content": f"⚠ レポート生成失敗\n```\n{msg[:1800]}\n```"}, timeout=30)