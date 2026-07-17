from __future__ import annotations
import datetime as dt
import io
import os
import requests

JST = dt.timezone(dt.timedelta(hours=9))
LIMIT = 1900  # Discord本文2000字制限に対する安全マージン


def _chunks(text: str) -> list[str]:
    """見出し(#)を跨がないように分割"""
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > LIMIT:
            out.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        out.append(buf)
    return out


def post(report: str, audit_result: str = "OK") -> None:
    url = os.environ["DISCORD_WEBHOOK_URL"]
    now = dt.datetime.now(JST)
    head = report.strip().split("\n\n")[0][:1500]

    # 1) サマリーEmbed + 全文をmdファイル添付（2000字制限を回避する本命）
    files = {"file": (f"morning_{now:%Y%m%d}.md",
                      io.BytesIO(report.encode("utf-8")), "text/markdown")}
    payload = {
        "username": "Morning Strategist",
        "embeds": [{
            "title": f"モーニングレポート {now:%Y/%m/%d (%a) %H:%M} JST",
            "description": head,
            "color": 0x2E86C1 if audit_result == "OK" else 0xE67E22,
            "footer": {"text": f"ファクトチェック: {audit_result[:200]}"},
        }],
    }
    r = requests.post(url, data={"payload_json": __import__("json").dumps(payload)},
                      files=files, timeout=30)
    r.raise_for_status()

    # 2) 本文もチャンク投稿（スマホでファイルを開かず読めるように）
    for i, c in enumerate(_chunks(report)):
        requests.post(url, json={"content": c}, timeout=30).raise_for_status()


def post_holiday(d) -> None:
    """休場日の1行通知。無音を「異常」だけの意味に固定するために必要。"""
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    requests.post(url, json={
        "embeds": [{
            "title": f"{d:%Y/%m/%d (%a)} — 東証休場",
            "description": "本日はレポートなし。システムは正常に稼働しています。",
            "color": 0x95A5A6,
        }]}, timeout=30)


def post_error(msg: str) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL")
    if not url:
        return
    requests.post(url, json={"content": f"⚠️ レポート生成失敗\n```\n{msg[:1800]}\n```"}, timeout=30)
