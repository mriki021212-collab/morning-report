"""
層2（発掘ユニバース）の母集団を作る。

この手法は本来スクリーナーなので、層1の13銘柄だけでは該当がほぼ出ず、検証用のサンプル数も
足りない。ここは「どの銘柄を判定対象にするか」だけを決める層で、判定そのものは
accumulation.py の仕事。

モードは config.yaml の accumulation.layers.universe.mode で切り替える:
  nikkei225    … Step2。規模テスト用の約225銘柄
  jpx_filtered … Step3。JPXの上場銘柄一覧にサイズ・流動性フィルタを掛けた本番ユニバース

取得できなければ必ず status を返す。銘柄リストを推測で埋めることは絶対にしない
（母集団が違えば結果の意味が変わるので、「取れなかった」と「該当ゼロ」は別物）。

■ 日経225の再配布について
日経のCSVには「本資料は日経の著作物であり、…無断で複写、複製、転載または流布することが
できません」という注記が付いている。このリポジトリは public なので、
  - キャッシュは out/ に置く（out/* は .gitignore 済み。ここを whitelist に足さないこと）
  - 構成銘柄の一覧そのものを out/accumulation.json やダッシュボードに出さない
の2点を守る。判定結果として個別銘柄のシグナル行が出るのは、リストの再配布ではない。
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib

JST = dt.timezone(dt.timedelta(hours=9))

_UA = {"User-Agent": "Mozilla/5.0 (morning-report/1.0)"}

# JPXの「市場・商品区分」のうち、株式ではない/対象外のもの。
# 実測(2026-08-31版・4441行)で存在を確認した区分名だけを書く。
_EXCLUDE_SEGMENTS = (
    "ETF・ETN",
    "REIT・ベンチャーファンド・カントリーファンド・インフラファンド",
    "PRO Market",
    "出資証券",
    "プライム（外国株式）",
    "スタンダード（外国株式）",
    "グロース（外国株式）",
)


def _is_tse_code(code: str) -> bool:
    """東証の銘柄コードか。4桁の英数字で、先頭は数字。

    2024年以降に上場した銘柄には英字を含むコードがある (285A キオクシア / 543A ARCHION)。
    `code.isdigit()` で判定すると、これらが理由も出さずに母集団から消える。
    ヘッダ行・著作権表示行・空行をここで一緒に落とす。
    """
    return (len(code) == 4 and code[0].isdigit()
            and all(c.isdigit() or c.isupper() for c in code))


def _cache_read(path: pathlib.Path, cache_hours: float) -> dict | None:
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        age = (dt.datetime.now(JST)
               - dt.datetime.fromisoformat(d["fetched_at"])).total_seconds() / 3600
        return d if age < cache_hours else None
    except Exception:
        return None


def _cache_write(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data, fetched_at=dt.datetime.now(JST).isoformat(timespec="seconds"))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------
# Step2: 日経225
# --------------------------------------------------------------------------
def fetch_nikkei225(url: str, out_dir: pathlib.Path, cache_hours: float = 24) -> dict:
    """日経が配布する構成銘柄ウエートCSVから225銘柄を取る。

    HTMLの構成銘柄ページ(indexes.nikkei.co.jp/nkave/index/component)は Cloudflare の
    ボット判定が入っていて中身が返らない（2026-09-06実測。200で返るのは challenge ページ）。
    こちらのCSVは直接取得できる。文字コードは cp932。
    """
    import requests

    cache_path = out_dir / "universe_nikkei225.json"
    c = _cache_read(cache_path, cache_hours)
    if c:
        return c

    try:
        r = requests.get(url, timeout=60, headers=_UA)
        r.raise_for_status()
        text = r.content.decode("cp932")
    except Exception as e:
        return {"status": f"取得失敗: {type(e).__name__}: {e}", "members": None}

    members, as_of, skipped = [], None, []
    for row in csv.DictReader(io.StringIO(text)):
        code = (row.get("コード") or "").strip()
        # 末尾に著作権表示の行が1行入る。コード列が空なのでここで落ちる。
        # isdigit() で弾いてはいけない: 東証の銘柄コードは英数字4桁で、
        # 285A(キオクシア) 543A(ARCHION) のように英字を含むものが実在する。
        # 数字のみに絞ると、この2銘柄が黙って母集団から消える(実測で発生させた)。
        if not _is_tse_code(code):
            # 落とした行は数えて持ち回る。黙って捨てると、CSVの形式が変わって
            # 半分しか読めなくなっても「そういう母集団」に見えてしまう。
            if code:
                skipped.append(code)
            continue
        as_of = as_of or (row.get("日付") or "").strip()
        members.append({"code": f"{code}.T", "name": (row.get("社名") or "").strip(),
                        "sector": (row.get("業種") or "").strip()})
    if not members:
        return {"status": "取得できたが構成銘柄を1件も解釈できなかった", "members": None}

    # 日経平均は225銘柄。大きく外れたらパースが壊れている可能性が高いので警告を残す。
    warn = (None if 200 <= len(members) <= 240 else
            f"構成銘柄が{len(members)}件。日経平均は225銘柄のはずで、パース漏れの疑いがある")
    data = {"status": "ok", "source": "日経平均株価 構成銘柄ウエート (日本経済新聞社)",
            "url": url, "as_of": as_of, "n": len(members), "members": members,
            "n_skipped": len(skipped), "skipped_codes": skipped[:20], "warning": warn,
            "redistribution": "日経の著作物。銘柄一覧を公開物に出さないこと。"}
    _cache_write(cache_path, data)
    return data


# --------------------------------------------------------------------------
# Step3: JPX上場銘柄一覧
# --------------------------------------------------------------------------
def fetch_jpx_listed(url: str, out_dir: pathlib.Path, cache_hours: float = 168) -> dict:
    """JPXが配布する上場銘柄一覧(xlsx)。全上場銘柄の区分・業種・規模区分を持つ。

    注意: 拡張子は .xlsx。.xls のURLは404になる（2026-09-06実測）。
    このファイルに時価総額は無い。サイズ条件は規模区分での絞り込み＋実データでの確認に分ける。
    """
    import pandas as pd
    import requests

    cache_path = out_dir / "universe_jpx_listed.json"
    c = _cache_read(cache_path, cache_hours)
    if c:
        return c

    try:
        r = requests.get(url, timeout=120, headers=_UA)
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content))
    except Exception as e:
        return {"status": f"取得失敗: {type(e).__name__}: {e}", "rows": None}

    need = {"コード", "銘柄名", "市場・商品区分", "規模区分", "33業種区分", "日付"}
    if not need.issubset(df.columns):
        missing = ", ".join(sorted(need - set(df.columns)))
        return {"status": f"列構成が想定と違う（欠落: {missing}）", "rows": None}

    rows = [{"code": f"{str(x['コード']).strip()}.T", "name": str(x["銘柄名"]).strip(),
             "segment": str(x["市場・商品区分"]).strip(),
             "size": str(x["規模区分"]).strip(),
             "industry": str(x["33業種区分"]).strip()}
            for _, x in df.iterrows()]
    data = {"status": "ok", "source": "JPX 上場銘柄一覧", "url": url,
            "as_of": str(df["日付"].iloc[0]), "n": len(rows), "rows": rows}
    _cache_write(cache_path, data)
    return data


def jpx_stock_candidates(listed: dict, exclude_sizes: list[str] | None = None
                         ) -> tuple[list[dict], dict[str, int]]:
    """上場銘柄一覧から「内国普通株」だけを残す。

    落とすもの:
      - ETF/ETN・REIT等・PRO Market・外国株式・出資証券（市場・商品区分で判別）
      - 5桁コードの優先株/社債型種類株。実測(2026-08-31版)で7銘柄あり、
        いずれも「プライム（内国株式）」に入っているため区分では落ちない。
        普通株ではないので出来高の意味が違う。4桁コードであることを条件にする。
      - exclude_sizes に挙げた規模区分（Core30/Large70 は時価総額の帯から明らかに外れる）

    返り値: (残った銘柄, 除外理由ごとの件数)
    何件をなぜ落としたかを数えて返す。母集団が想定と違う時に原因を追えるようにするため。
    """
    out: list[dict] = []
    dropped = {"segment": 0, "not_4char_code": 0, "size": 0}
    for r in listed.get("rows") or []:
        if r["segment"] in _EXCLUDE_SEGMENTS:
            dropped["segment"] += 1
            continue
        if not _is_tse_code(r["code"].replace(".T", "")):
            dropped["not_4char_code"] += 1
            continue
        if exclude_sizes and r["size"] in exclude_sizes:
            dropped["size"] += 1
            continue
        out.append(r)
    return out, dropped


# --------------------------------------------------------------------------
# 実データでの絞り込み（売買代金・履歴長）
# --------------------------------------------------------------------------
def screen_by_price_data(frames: dict, min_turnover_oku: float, turnover_days: int,
                         min_bdays: int) -> tuple[list[str], dict[str, str]]:
    """取得済みのOHLCVで流動性と履歴長を検査する。

    売買代金は「終値 × 出来高」で計算する。JPXの一覧には無いが、価格データから
    そのまま出せる（推定ではなく実測値）。単位は億円。

    返り値: (通過したコード, {落ちたコード: 理由})
    """
    passed, rejected = [], {}
    for code, df in frames.items():
        if len(df) < min_bdays:
            rejected[code] = f"履歴{len(df)}営業日 < 必要{min_bdays}営業日"
            continue
        tail = df.tail(turnover_days)
        if tail["Volume"].isna().any() or tail["Close"].isna().any():
            rejected[code] = f"直近{turnover_days}日に出来高または終値の欠損がある"
            continue
        turnover_oku = float((tail["Close"] * tail["Volume"]).mean()) / 1e8
        if turnover_oku < min_turnover_oku:
            rejected[code] = (f"平均売買代金 {turnover_oku:.2f}億円 "
                              f"< 必要{min_turnover_oku}億円")
            continue
        passed.append(code)
    return passed, rejected


def screen_by_market_cap(codes: list[str], min_oku: float, max_oku: float
                         ) -> tuple[list[str], dict[str, str], dict[str, float]]:
    """時価総額でサイズ帯を絞る。

    大型すぎると機関の買いが出来高に痕跡を残さず、小型すぎると個人の単発注文で
    スパイクが立ちノイズになる。そのための帯。単位は億円。

    yfinance の fast_info.market_cap を使う。取れなかった銘柄は「帯の中かもしれないが
    確認できない」ので通さない（推定で埋めない）。

    注意: FastInfo は属性が snake_case、dictキーが camelCase という二重の顔を持つ。
    `fast_info.get("market_cap")` は例外を出さずに None を返すので、これを使うと
    「全銘柄で時価総額が取れない」状態が無言で起きる（実測で発生させた）。
    属性アクセス `.market_cap` を使うこと。
    """
    import yfinance as yf

    passed, rejected, caps = [], {}, {}
    for code in codes:
        try:
            mc = yf.Ticker(code).fast_info.market_cap
        except Exception as e:
            rejected[code] = f"時価総額を取得できない: {type(e).__name__}"
            continue
        if not mc:
            rejected[code] = "時価総額を取得できない（応答に含まれない）"
            continue
        oku = float(mc) / 1e8
        caps[code] = round(oku, 1)
        if oku < min_oku:
            rejected[code] = f"時価総額 {oku:,.0f}億円 < {min_oku}億円"
        elif oku > max_oku:
            rejected[code] = f"時価総額 {oku:,.0f}億円 > {max_oku}億円"
        else:
            passed.append(code)
    return passed, rejected, caps


# --------------------------------------------------------------------------
# Step3: 層2ユニバースの構築
# --------------------------------------------------------------------------
def build_jpx_universe(cfg: dict, out_dir: pathlib.Path, verbose: bool = True) -> dict:
    """JPXの上場銘柄一覧にフィルタを掛けて層2ユニバースを作り、キャッシュに書く。

    重い処理（3,600銘柄の株価取得＋数百銘柄の時価総額照会）なので、毎回の判定では
    走らせない。週1回のフル走査の前に1度だけ実行し、結果を
    out/universe_jpx_filtered.json に置く。members() はそれを読むだけ。

    絞る順番には理由がある。安い条件から先に掛けて、高い条件に渡す件数を減らす:
      1. 区分・コード・規模区分   … ファイルだけで判定できる。ネットワーク不要
      2. 履歴長・平均売買代金     … 一括取得した株価から計算できる。追加リクエストなし
      3. 時価総額                 … 1銘柄1リクエスト。ここに来る件数を最小にしたい
    """
    import collect

    ucfg = cfg["accumulation"]["layers"]["universe"]
    f = ucfg["filters"]
    fetch_cfg = ucfg.get("fetch") or {}
    t0 = dt.datetime.now(JST)

    listed = fetch_jpx_listed(ucfg["sources"]["jpx_listed"]["url"], out_dir,
                              ucfg.get("jpx_cache_hours", 168))
    if listed.get("rows") is None:
        return {"status": f"ユニバースを取得できていません: {listed.get('status')}",
                "members": None}

    cands, dropped = jpx_stock_candidates(listed, f.get("exclude_sizes"))
    if verbose:
        print(f"1. JPX一覧 {listed['n']}件 → 一次フィルタ後 {len(cands)}件 {dropped}")

    codes = [c["code"] for c in cands]
    frames, fetch_failed = collect.fetch_ohlcv_batch(
        codes, period=fetch_cfg.get("period", "3y"),
        chunk=fetch_cfg.get("chunk", 40), pause=fetch_cfg.get("pause_sec", 0.0))
    if verbose:
        print(f"2. 株価取得 成功{len(frames)} / 失敗{len(fetch_failed)}")

    liquid, rejected_px = screen_by_price_data(
        frames, f["avg_turnover_min_oku"], f["avg_turnover_days"],
        f["min_listed_bdays"])
    if verbose:
        n_short = sum(1 for v in rejected_px.values() if "履歴" in v)
        print(f"3. 履歴長・売買代金 → {len(liquid)}件 "
              f"(履歴不足{n_short} / 流動性不足{len(rejected_px)-n_short})")

    passed, rejected_mc, caps = screen_by_market_cap(
        liquid, f["market_cap_min_oku"], f["market_cap_max_oku"])
    if verbose:
        print(f"4. 時価総額 {f['market_cap_min_oku']}〜{f['market_cap_max_oku']}億円 "
              f"→ {len(passed)}件")

    by_code = {c["code"]: c for c in cands}
    members_ = [{"code": c, "name": by_code[c]["name"],
                 "industry": by_code[c].get("industry"),
                 "size": by_code[c].get("size"),
                 "market_cap_oku": caps.get(c)}
                for c in passed]
    members_.sort(key=lambda m: m["code"])

    data = {
        "status": "ok" if members_ else "フィルタを通過した銘柄が0件",
        "source": listed.get("source"), "url": listed.get("url"),
        "as_of": listed.get("as_of"),
        "built_at": t0.isoformat(timespec="seconds"),
        "elapsed_sec": round((dt.datetime.now(JST) - t0).total_seconds(), 1),
        "filters": f,
        "funnel": {
            "listed_total": listed["n"],
            "after_primary": len(cands),
            "primary_dropped": dropped,
            "price_fetched": len(frames),
            "price_fetch_failed": len(fetch_failed),
            "after_liquidity": len(liquid),
            "after_market_cap": len(passed),
        },
        # 落ちた理由は件数だけ残す。3,600件ぶんの理由を全部書くとファイルが巨大になる。
        "rejected_sample": dict(list(rejected_mc.items())[:5]),
        "n": len(members_),
        "members": members_,
    }
    _cache_write(out_dir / "universe_jpx_filtered.json", data)
    return data


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def members(cfg: dict, out_dir: pathlib.Path) -> dict:
    """config の mode に従って層2の母集団を返す。

    返り値: {"status": None|str, "mode": ..., "members": [{code,name}] | None, ...}
    status が非Noneなら母集団を作れていない = 層2は「未構築」であって「該当ゼロ」ではない。
    """
    ucfg = (((cfg.get("accumulation") or {}).get("layers") or {}).get("universe") or {})
    if not ucfg.get("enabled"):
        return {"status": "ユニバース未構築（config で enabled: false）",
                "mode": ucfg.get("mode"), "members": None}

    mode = ucfg.get("mode")
    srcs = ucfg.get("sources") or {}
    cache_h = ucfg.get("cache_hours", 24)

    if mode == "nikkei225":
        s = srcs.get("nikkei225") or {}
        d = fetch_nikkei225(s.get("url"), out_dir, cache_h)
        if d.get("members") is None:
            return {"status": f"ユニバースを取得できていません: {d.get('status')}",
                    "mode": mode, "members": None}
        return {"status": None, "mode": mode, "source": d.get("source"),
                "as_of": d.get("as_of"), "members": d["members"],
                "n": len(d["members"]),
                "publishable": False}   # 日経の著作物。一覧を公開物に出さない

    if mode == "jpx_filtered":
        # 構築は build_jpx_universe() が別に走らせる（重いので毎回はやらない）。
        # ここは出来上がったものを読むだけ。無ければ「未構築」であって「該当ゼロ」ではない。
        path = out_dir / "universe_jpx_filtered.json"
        built = _cache_read(path, ucfg.get("universe_ttl_hours", 24 * 8))
        if built is None:
            why = ("まだ構築されていません" if not path.exists()
                   else "構築済みだが古くなっています")
            return {"status": f"ユニバース未構築（{why}。"
                              f"src/universe_build.py を実行してください）",
                    "mode": mode, "members": None}
        if built.get("members") is None:
            return {"status": f"ユニバースを取得できていません: {built.get('status')}",
                    "mode": mode, "members": None}
        return {"status": None, "mode": mode, "source": built.get("source"),
                "as_of": built.get("as_of"), "built_at": built.get("built_at"),
                "members": built["members"], "n": built["n"],
                "funnel": built.get("funnel"), "publishable": True}

    return {"status": f"未知のユニバースmode: {mode!r}", "mode": mode, "members": None}
