from __future__ import annotations
import json
import os
import pathlib
import anthropic

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSTEM = (ROOT / "prompts" / "system.md").read_text(encoding="utf-8")


def write_report(facts: dict, model: str, max_tokens: int) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user = (
        "以下の FACTS のみを根拠にモーニングレポートを作成せよ。\n"
        "FACTS に無い数値は書かないこと。不足はweb_searchでイベント/ニュースの"
        "一次情報のみ補い、補った箇所は出典URLを必ず付すこと。\n\n"
        "```json\n" + json.dumps(facts, ensure_ascii=False, indent=1, default=str) + "\n```"
    )

    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
        messages=[{"role": "user", "content": user}],
    )
    return "\n".join(b.text for b in resp.content if b.type == "text")


def audit(report: str, facts: dict, model: str) -> str:
    """検証パス: レポート内の数値がFACTSに存在するかを別コールで突き合わせる"""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=("あなたはファクトチェッカー。レポート中の数値を1つずつFACTSと突き合わせ、"
                "FACTSに存在しない/矛盾する数値のみを列挙せよ。問題が無ければ `OK` の2文字だけ返せ。"
                "説明・前置きは不要。"),
        messages=[{"role": "user", "content":
                   f"# REPORT\n{report}\n\n# FACTS\n```json\n"
                   + json.dumps(facts, ensure_ascii=False, default=str) + "\n```"}],
    )
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()
