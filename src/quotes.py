"""
場中の保有銘柄ライブ株価を out/quotes.json に書き出す。

dashboard.py が書く out/dashboard.json は main.py 実行時点（朝寄り付き前）の
スナップショットで、日中は更新されない。このモジュールは日中に繰り返し実行し、
terminal_dashboard.html のウォッチリストに「今何秒前の値か」を表示できるようにする。

取得できなかった銘柄は quotes.json に一切書かない（null/前回値/推定値で埋めない）。
理由: 285A（キオクシアHD, 2024年12月上場）はyfinanceの分足取得が銘柄・時間帯によって
失敗することがある。失敗を「取得できていません」として明示するのはHTML側の責務で、
このスクリプトは「取れた事実だけ」を書く。
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import collect  # noqa: E402
import yaml  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
JST = dt.timezone(dt.timedelta(hours=9))


def load_codes() -> list[str]:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    return [h["code"] for h in cfg["holdings"]]


def fetch_once(codes: list[str]) -> dict:
    quotes = {}
    failed = []
    for code in codes:
        q = collect.live_quote(code)
        if q and q.get("price") is not None and q.get("ts_jst"):
            quotes[code] = {"price": q["price"], "ts_jst": q["ts_jst"]}
        else:
            failed.append(code)
    return {
        "generated_at_jst": dt.datetime.now(JST).isoformat(),
        "quotes": quotes,
        "failed": failed,  # 取得できなかった銘柄コード。デバッグ用であり値の穴埋めには使わない
    }


def write(out_dir: pathlib.Path, payload: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "quotes.json.tmp"
    dst = out_dir / "quotes.json"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(dst)  # 読み取り中の中途半端なJSONを避けるための原子的置換


def main() -> None:
    ap = argparse.ArgumentParser(description="保有銘柄のライブ株価を out/quotes.json に書き出す")
    ap.add_argument("--once", action="store_true", help="1回だけ取得して終了（デフォルトの挙動と同じ）")
    ap.add_argument("--interval", type=int, default=0,
                     help="この秒数間隔で繰り返し取得する。指定しなければ1回だけ実行して終了する。")
    args = ap.parse_args()

    codes = load_codes()
    out_dir = ROOT / "out"

    def run_one():
        payload = fetch_once(codes)
        write(out_dir, payload)
        got = list(payload["quotes"].keys())
        miss = payload["failed"]
        ts = payload["generated_at_jst"]
        print(f"[{ts}] 取得成功: {got or '(なし)'} / 取得できず: {miss or '(なし)'}")

    if args.interval and not args.once:
        print(f"{args.interval}秒間隔で繰り返します。Ctrl+Cで停止。")
        while True:
            run_one()
            time.sleep(args.interval)
    else:
        run_one()


if __name__ == "__main__":
    main()
