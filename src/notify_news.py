"""
ニュースまとめを Discord に投げる。株レポート(notify.py)とは別チャンネル・別Webhook。

Webhook は環境変数 NEWS_DISCORD_WEBHOOK_URL から読む。
**絶対にコードや config.yaml に書かないこと。** このリポジトリは public で、
一度コミットしたWebhookは履歴から消せない。

notify.py と分けてある理由:
  - 送り先が違う（株の話とニュースをチャンネルで分けたい、というのが元の要望）
  - 投稿が落ちても株レポートを巻き込まない
  - 1回の実行で1メッセージ、という notify.py の約束をこちらでも独立に守る
"""
from __future__ import annotations

import datetime as dt
import json
import os

import requests

JST = dt.timezone(dt.timedelta(hours=9))
DASHBOARD_URL = "https://mriki021212-collab.github.io/morning-report/news_dashboard.html"

BLUE = 0x3498DB
ORANGE = 0xE67E22
GRAY = 0x95A5A6

ENV = "NEWS_DISCORD_WEBHOOK_URL"

# 見出しの鮮度。これより古いデータで投稿すると「今のニュース」に見えてしまう。
MAX_AGE_MIN = 180


def _age_min(generated_at: str) -> float | None:
    try:
        return (dt.datetime.now(JST)
                - dt.datetime.fromisoformat(generated_at)).total_seconds() / 60
    except (ValueError, TypeError):
        return None


def _market_line(markets: list[dict]) -> str:
    """先頭に出す相場の一行。取れなかった指標は飛ばさず「取得不可」と書く。"""
    want = ("日経平均", "TOPIX", "ドル円", "米10年金利", "SOX指数")
    by = {m["name"]: m for m in markets}
    out = []
    for n in want:
        m = by.get(n)
        if not m or m.get("close") is None:
            out.append(f"{n} 取得不可")
            continue
        c, p = m["close"], m.get("chg_pct")
        arrow = "🔺" if (p or 0) > 0 else "🔻" if (p or 0) < 0 else "➖"
        pct = f"{arrow}{abs(p):.2f}%" if p is not None else "―"
        out.append(f"**{n}** {c:,.2f} {pct}")
    return " ／ ".join(out)


def _jgb_line(jgb: dict) -> str:
    if not jgb or jgb.get("status"):
        return f"国債金利: {(jgb or {}).get('status', '取得できていません')}"
    c = jgb.get("curve", {})
    body = " / ".join(f"{k} {v:.3f}%" for k, v in c.items())
    tail = "  ⚠️更新が止まっている可能性" if jgb.get("stale") else ""
    return f"**国債利回り**（{jgb.get('base_date')}時点）{body}{tail}"


def _headline_field(name: str, rows: list[dict], limit: int) -> dict | None:
    """1カテゴリぶんの見出し。1024字上限に収まるところまで入れ、切ったら明示する。"""
    if not rows:
        return None
    lines, total, kept = [], 0, 0
    for r in rows[:limit]:
        tag = {"高": "🔴", "中": "🟡", "低": "⚪"}.get(r.get("tier"), "⚪")
        t = r["title"].replace("[", "［").replace("]", "］")  # リンク記法を壊さない
        ln = f"{tag} [{t}]({r['link']}) — {r.get('source','')}" if r.get("link") \
             else f"{tag} {t} — {r.get('source','')}"
        if total + len(ln) + 1 > 950:
            break
        lines.append(ln)
        total += len(ln) + 1
        kept += 1
    if not lines:
        return None
    if len(rows) > kept:
        lines.append(f"… 他{len(rows) - kept}件はダッシュボードに")
    return {"name": f"{name}（{len(rows)}件）", "value": "\n".join(lines), "inline": False}


def build_payload(d: dict) -> dict:
    h = d.get("headlines") or {}
    items = h.get("items") or []
    failed = d.get("failed") or []
    gen = d.get("generated_at", "")
    age = _age_min(gen)
    stale = age is not None and age > MAX_AGE_MIN

    # 色は状態を表す。古い/取得失敗を通常色で出すと「正常に見える」のが一番危ない。
    if h.get("status") == "全ソース取得失敗" or stale:
        color = ORANGE
    elif not items:
        color = GRAY
    else:
        color = BLUE

    desc = [_market_line(d.get("markets") or []), _jgb_line(d.get("jgb") or {})]
    if stale:
        desc.insert(0, f"⚠️ **このデータは{age/60:.1f}時間前のものです**"
                       f"（生成 {gen[:16]}）。最新の相場ではありません。")
    if h.get("status") == "全ソース取得失敗":
        desc.append("⚠️ **RSSに1本も到達できていません。報道がゼロなのではありません。**")
    elif h.get("status") == "該当する記事なし":
        desc.append("株に関係する見出しは直近に該当なし（取得は成功しています）。")

    fields = []
    # 重要度「高」は横断で先に出す。カテゴリより先に、読ませたいものを上に置く。
    high = [i for i in items if i.get("tier") == "高"]
    f = _headline_field("🔴 重要度 高", high, 8)
    if f:
        fields.append(f)
    for cat, rows in (h.get("by_category") or {}).items():
        rest = [r for r in rows if r.get("tier") != "高"]
        f = _headline_field(cat, rest, 6)
        if f:
            fields.append(f)
        if len(fields) >= 8:      # embedのfieldは25まで。余裕を持って切る
            break

    if failed:
        fields.append({"name": "⚠️ 取得できなかったソース",
                       "value": "\n".join(f"・{x}" for x in failed)[:1024],
                       "inline": False})
    fields.append({"name": "🔗 ニュースダッシュボード",
                   "value": f"[全件を見る]({DASHBOARD_URL})", "inline": False})

    # 失敗件数は failed の実数を出す。SOURCES の成否だけを数えると、
    # 恒常的に403のソース(NHK/東洋経済等)が数に入らず「0件失敗」と出てしまい、
    # 下の「取得できなかったソース」欄と矛盾する。
    n_ok = h.get("n_sources_ok")
    now = dt.datetime.now(JST)
    return {
        "username": "News Digest",
        "embeds": [{
            "title": f"📰 新聞まとめ {now:%Y/%m/%d (%a) %H:%M} JST",
            "url": DASHBOARD_URL,
            "description": "\n".join(desc)[:4000],
            "color": color,
            "fields": fields,
            "footer": {"text": f"見出し{len(items)}件 / RSS {n_ok}件成功"
                               f"{f'・{len(failed)}件失敗' if failed else ''}"
                               " ・自動生成・投資助言ではありません"},
        }],
    }


def post(d: dict) -> None:
    url = os.getenv(ENV)
    if not url:
        raise RuntimeError(
            f"{ENV} が未設定。Webhookは環境変数からのみ読む（リポジトリに書かない）。")
    payload = build_payload(d)
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"Discord投稿に失敗: HTTP {r.status_code} {r.text[:200]}")


if __name__ == "__main__":
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[1]
    data = json.loads((root / "out" / "newsdash.json").read_text(encoding="utf-8"))
    if "--dry-run" in sys.argv:
        print(json.dumps(build_payload(data), ensure_ascii=False, indent=1))
    else:
        post(data)
        print("posted.")
