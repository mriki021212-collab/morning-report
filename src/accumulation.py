"""
機関投資家の買い集め（アキュムレーション）検出。

探しているのは「出来高が増えた株」ではなく「出来高が増えた後も売られない株」。
機関は一度に買えないので何日もかけて集める。その痕跡は
  ・普段の2倍3倍の出来高を伴って上昇した日があり
  ・その日の終値が高値近辺で終わり（＝売られた株を誰かが買い続けている）
  ・その後も押し込まれず、窓を埋めず、値幅が狭くなっていく
という形で出る。逆に出来高が急増したのに上ひげをつけて安値近辺で終わる日は、
大口が買っているのではなく売っている可能性がある。

このモジュールの責任範囲は「数値を出すところまで」。買え/売れは一切書かない。
閾値は全て config.yaml の accumulation: にあり、ここには数値を直書きしない。

データが無い時の扱い（このリポジトリの最重要ルール）:
  - H == L の日の終値位置(CRP)は None。0.5 などで埋めない。
  - 履歴が min_history_days に満たない銘柄は値を一切出さず、理由だけを出す。
  - スパイクから followup_days ぶんの足が埋まっていなければ「追跡中」であって
    「④不成立」ではない。この2つを同じ表示にしない。
  - 決算日が取れていない期間のスパイクは「決算スパイクではない」ではなく
    「決算かどうか判別不可」。
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import pathlib

import numpy as np
import pandas as pd

JST = dt.timezone(dt.timedelta(hours=9))
STATE_FILE = "accumulation_state.json"


# --------------------------------------------------------------------------
# パラメータ
# --------------------------------------------------------------------------
class Params:
    """config.yaml の accumulation: をそのまま持つだけの入れ物。既定値は置かない。

    既定値を持たせるとキーを消した時に黙って別の閾値で動いてしまう。
    設定が欠けているなら落ちたほうがよい。
    """

    def __init__(self, cfg: dict):
        a = cfg.get("accumulation")
        if not a:
            raise KeyError("config.yaml に accumulation: が無い")
        self.raw = a
        self.min_history_days = a["min_history_days"]
        self.vol_spike_ratio = a["vol_spike_ratio"]
        self.vol_sma_days = a["vol_sma_days"]
        self.vol_sma_exclude_today = a["vol_sma_exclude_today"]
        self.crp_high = a["crp_high"]
        self.crp_low = a["crp_low"]
        self.upper_wick_min = a["upper_wick_min"]
        self.ud_days = a["ud_days"]
        self.ud_min = a["ud_min"]
        self.followup_days = a["followup_days"]
        self.drawdown_r_mult = a["drawdown_r_mult"]
        self.range_contraction = a["range_contraction"]
        self.range_base_days = a["range_base_days"]
        self.scan_days = a["scan_days"]
        self.candidate_keep_days = a["candidate_keep_days"]
        em = a["earnings_match"]
        self.earn_after_close = em["after_close_hhmm"]
        self.earn_window_bdays = em["window_bdays"]
        self.earn_cache_hours = em["cache_hours"]
        self.earn_tdnet_limit = em["tdnet_limit"]
        self.layers = a["layers"]

    def as_dict(self) -> dict:
        return self.raw


def _f(x):
    """NaN/Inf を None に落とす。JSONに NaN を出さないため。"""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if (np.isnan(x) or np.isinf(x)) else round(x, 4)


# --------------------------------------------------------------------------
# 指標
# --------------------------------------------------------------------------
def prepare(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """日足OHLCVから、判定に要る列だけを足した DataFrame を返す。

    ここで計算するのは B の ①②のみ。③④は窓が要るので別関数。
    """
    d = df.copy()
    rng = d["High"] - d["Low"]
    d["rng"] = rng
    ok = rng > 0  # H == L の日は終値位置が定義できない

    # ② 終値位置 CRP = (C - L) / (H - L)
    d["crp"] = np.where(ok, (d["Close"] - d["Low"]) / rng, np.nan)
    # 仕様どおりの上ひげ (H - C)/(H - L)。定義上これは 1 - CRP に等しい。
    d["wick_from_close"] = np.where(ok, (d["High"] - d["Close"]) / rng, np.nan)
    # ローソク足の一般的な「上ひげ」＝実体の上端からの長さ。判定には使わず併記だけする。
    body_top = d[["Open", "Close"]].max(axis=1)
    d["upper_shadow"] = np.where(ok, (d["High"] - body_top) / rng, np.nan)

    # ① 出来高スパイク
    v = pd.to_numeric(d["Volume"], errors="coerce")
    base = v.shift(1) if p.vol_sma_exclude_today else v
    sma = base.rolling(p.vol_sma_days).mean()
    d["vol_sma"] = sma
    # 平均が0（=直近20日まったく出来高が無い）なら比率は定義できない。1で割らない。
    d["vol_ratio"] = np.where((sma > 0) & sma.notna() & v.notna(), v / sma, np.nan)

    d["chg"] = d["Close"].diff()
    d["rng_pct"] = np.where(d["Close"] > 0, rng / d["Close"], np.nan)
    return d


def ud_ratio(d: pd.DataFrame, p: Params) -> dict:
    """③ 直近 ud_days 日の Σ(上昇日の出来高)/Σ(下落日の出来高)。

    補助スコア。単独では候補判定に使わない（Bの但し書きのとおり）。
    """
    if len(d) < p.ud_days + 1:
        return {"value": None, "pass": None,
                "status": f"直近{p.ud_days}日ぶんの足が無い（{len(d)}日）"}
    tail = d.tail(p.ud_days)
    v = pd.to_numeric(tail["Volume"], errors="coerce")
    if v.isna().any() or tail["chg"].isna().any():
        return {"value": None, "pass": None, "status": "期間内に出来高または終値の欠損がある"}
    up = float(v[tail["chg"] > 0].sum())
    dn = float(v[tail["chg"] < 0].sum())
    if dn <= 0:
        # 下落日が1日も無い/出来高ゼロ。比率は無限大であって「非常に強い」ではない。
        return {"value": None, "pass": None, "days": p.ud_days,
                "status": "期間内に下落日の出来高が無く、比率を算出できない"}
    r = up / dn
    return {"value": _f(r), "pass": bool(r >= p.ud_min), "days": p.ud_days,
            "up_volume": _f(up), "down_volume": _f(dn), "threshold": p.ud_min,
            "status": None}


def followup(d: pd.DataFrame, i: int, p: Params) -> dict:
    """④ スパイク後の耐性。i はスパイク日の位置インデックス。

    3条件すべてを満たしたときだけ pass=True。1つでも判定材料が無ければ
    pass=None（不成立ではない）を返す。
    """
    n = p.followup_days
    after = d.iloc[i + 1: i + 1 + n]
    if len(after) < n:
        return {"state": "tracking", "elapsed": int(len(after)), "needed": n, "pass": None,
                "status": f"スパイク後{len(after)}/{n}営業日。判定に必要な日数が不足"}

    row = d.iloc[i]
    p0, r, spike_open = float(row["Close"]), float(row["rng"]), float(row["Open"])
    out: dict = {"state": "evaluated", "elapsed": n, "needed": n}

    # 押し込み耐性: 以降N日の最安値 >= P0 - 0.5R
    if r > 0:
        floor_ = p0 - p.drawdown_r_mult * r
        low = float(after["Low"].min())
        out["drawdown"] = {"min_low": _f(low), "floor": _f(floor_),
                           "pass": bool(low >= floor_)}
    else:
        # 値幅ゼロの日はそもそも候補にならない（CRPがNone）。到達しない想定だが埋めない。
        out["drawdown"] = {"min_low": None, "floor": None, "pass": None,
                           "status": "スパイク日の値幅が0で基準を作れない"}

    # 窓を埋めない: 以降N日の終値がスパイク日の始値を割らない
    min_close = float(after["Close"].min())
    out["gap_hold"] = {"min_close": _f(min_close), "spike_open": _f(spike_open),
                       "pass": bool(min_close >= spike_open)}

    # レンジ収縮: 直近N日平均(H-L)/C < スパイク前M日平均 * range_contraction
    m = p.range_base_days
    before = d.iloc[max(0, i - m): i]
    if len(before) < m or before["rng_pct"].isna().any() or after["rng_pct"].isna().any():
        out["range_contraction"] = {"pass": None,
                                    "status": f"スパイク前{m}日の値幅データが揃わない"}
    else:
        a_mean, b_mean = float(after["rng_pct"].mean()), float(before["rng_pct"].mean())
        thr = b_mean * p.range_contraction
        out["range_contraction"] = {"after_mean_pct": _f(a_mean * 100),
                                    "before_mean_pct": _f(b_mean * 100),
                                    "threshold_pct": _f(thr * 100),
                                    "pass": bool(a_mean < thr) if b_mean > 0 else None,
                                    "status": None if b_mean > 0 else "前M日の平均値幅が0"}

    checks = [out["drawdown"]["pass"], out["gap_hold"]["pass"],
              out["range_contraction"]["pass"]]
    # 3条件のうち何本通ったか。「1つも通らない」と「2つ通って1つ落ちた」を
    # 同じ『不成立』に潰すと、閾値のどれが効きすぎているのかが画面から見えなくなる。
    out["passed_count"] = sum(1 for c in checks if c is True)
    out["checked_count"] = sum(1 for c in checks if c is not None)
    out["failed_conditions"] = [k for k in ("drawdown", "gap_hold", "range_contraction")
                                if out[k]["pass"] is False]
    if any(c is None for c in checks):
        out["pass"] = None
        out["status"] = "判定材料が揃わない条件がある"
    else:
        out["pass"] = all(checks)
        out["status"] = None
    return out


# --------------------------------------------------------------------------
# 決算スパイクの判別（Aの6）
# --------------------------------------------------------------------------
def reaction_positions(d: pd.DataFrame, earn: dict, p: Params) -> tuple[set[int] | None, str]:
    """決算発表が「市場に効く日」の位置インデックス集合を返す。

    15:00以降の開示はその日の場が終わっているので、反応するのは翌営業日。
    返り値の第1要素が None なら「決算日を取得できていない」。空集合とは意味が違う。
    """
    if not earn or earn.get("announcements") is None:
        return None, (earn or {}).get("status") or "決算日を取得していない"
    hh, mm = (int(x) for x in p.earn_after_close.split(":"))
    cutoff = dt.time(hh, mm)
    dates = [ts.date() for ts in d.index]
    pos_of = {dd: i for i, dd in enumerate(dates)}
    marks: set[int] = set()
    for a in earn["announcements"]:
        when = dt.datetime.fromisoformat(a["datetime_jst"])
        day = when.date()
        # 15:00以降の開示、または場が開いていない日の開示（休日開示）は、
        # 反応するのは「開示日より後の最初の足」。日付列は昇順なので二分探索でよい
        # （線形走査だと 900銘柄 × 数千日 × 数十回で効いてくる）。
        if when.timetz().replace(tzinfo=None) >= cutoff or day not in pos_of:
            j = bisect.bisect_right(dates, day)
            if j < len(dates):
                marks.add(j)
        else:
            marks.add(pos_of[day])
    w = p.earn_window_bdays
    widened = {m + k for m in marks for k in range(-w, w + 1)}
    return widened, "ok"


# --------------------------------------------------------------------------
# 銘柄単位の判定
# --------------------------------------------------------------------------
def analyze(code: str, name: str, df: pd.DataFrame, earn: dict | None,
            p: Params, layer: str, prepared: pd.DataFrame | None = None) -> dict:
    base = {"code": code, "name": name, "layer": layer}
    if df is None or df.empty:
        return dict(base, status="株価データを取得できていません", history_days=0)

    need = {"Open", "High", "Low", "Close", "Volume"}
    if not need.issubset(df.columns):
        missing = ", ".join(sorted(need - set(df.columns)))
        return dict(base, status=f"OHLCVが揃っていません（欠落: {missing}）",
                    history_days=int(len(df)))

    if len(df) < p.min_history_days:
        return dict(base, status=f"判定に必要な日数が不足（{len(df)}営業日 / "
                                 f"必要{p.min_history_days}営業日）",
                    history_days=int(len(df)),
                    as_of=df.index[-1].strftime("%Y-%m-%d"))

    d = prepared if prepared is not None else prepare(df, p)
    marks, earn_status = reaction_positions(d, earn or {}, p)
    covers_from = (earn or {}).get("covers_from")

    scan_from = max(p.vol_sma_days + 1, len(d) - p.scan_days)
    spikes = []
    for i in range(scan_from, len(d)):
        vr = d["vol_ratio"].iat[i]
        if not (vr == vr) or vr < p.vol_spike_ratio:   # NaN もここで落ちる
            continue
        row = d.iloc[i]
        sdate = d.index[i].strftime("%Y-%m-%d")
        crp = _f(row["crp"])

        # 決算スパイクか通常スパイクか。取得範囲外なら判別不可（＝通常とは言わない）。
        if marks is None:
            is_earn, earn_reason = None, f"決算日を取得できていません（{earn_status}）"
        elif covers_from and sdate < covers_from:
            is_earn, earn_reason = None, f"決算日の取得範囲（{covers_from}以降）より古い"
        else:
            is_earn, earn_reason = (i in marks), None

        sp = {
            "date": sdate,
            "vol_ratio": _f(vr),
            "vol_sma20": _f(row["vol_sma"]),
            "volume": _f(row["Volume"]),
            "open": _f(row["Open"]), "high": _f(row["High"]),
            "low": _f(row["Low"]), "close": _f(row["Close"]),
            "chg_pct": _f((row["Close"] / d["Close"].iat[i - 1] - 1) * 100) if i else None,
            "crp": crp,
            "wick_from_close": _f(row["wick_from_close"]),
            "upper_shadow": _f(row["upper_shadow"]),
            "earnings_spike": is_earn,
            "earnings_spike_status": earn_reason,
            "range_zero": bool(row["rng"] <= 0),
        }

        if crp is None:
            # H == L。終値位置が定義できないので候補にも逆アラートにもしない。
            sp["kind"] = "判定不可"
            sp["kind_reason"] = "値幅が0（H==L）のため終値位置を算出できない"
            spikes.append(sp)
            continue

        if crp >= p.crp_high:
            sp["kind"] = "候補"
            sp["followup"] = followup(d, i, p)
            if sp["followup"].get("pass") is True:
                sp["kind"] = "集積の疑い"
            elif sp["followup"].get("state") == "tracking":
                sp["kind"] = "候補（追跡中）"
            elif sp["followup"].get("pass") is False:
                sp["kind"] = "候補（④不成立）"
            else:
                sp["kind"] = "候補（④判定不可）"
        elif crp <= p.crp_low and _f(row["wick_from_close"]) is not None \
                and row["wick_from_close"] >= p.upper_wick_min:
            sp["kind"] = "売り抜けの疑い"
        else:
            sp["kind"] = "高出来高（終値位置は中間）"
        spikes.append(sp)

    last = d.iloc[-1]
    return dict(
        base,
        status=None,
        as_of=d.index[-1].strftime("%Y-%m-%d"),
        history_days=int(len(d)),
        scan_days=int(len(d) - scan_from),
        earnings_source={"status": earn_status, "covers_from": covers_from,
                         "truncated": (earn or {}).get("truncated")},
        ud=ud_ratio(d, p),
        latest={"date": d.index[-1].strftime("%Y-%m-%d"),
                "vol_ratio": _f(last["vol_ratio"]), "crp": _f(last["crp"]),
                "close": _f(last["Close"]), "volume": _f(last["Volume"])},
        spikes=spikes,
    )


# --------------------------------------------------------------------------
# 候補フラグの永続化（E: 週1フル走査 → 平日は候補だけ追跡）
# --------------------------------------------------------------------------
def load_state(out_dir: pathlib.Path) -> dict:
    path = out_dir / STATE_FILE
    if not path.exists():
        return {"candidates": {}}
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        # 壊れた状態ファイルを空とみなすと「候補ゼロ」に化ける。理由を持たせる。
        return {"candidates": {}, "load_error": f"{type(e).__name__}: {e}"}
    s.setdefault("candidates", {})
    return s


def update_state(state: dict, results: list[dict], p: Params, today: dt.date) -> dict:
    """候補スパイクを状態ファイルに反映する。

    層2は毎日フル走査しない（数百銘柄を毎朝叩くと落ちる）。週1回の走査で立った
    候補フラグをここに残し、平日はこのリストの銘柄だけ追跡する。
    """
    cands = state.get("candidates", {})
    for r in results:
        if r.get("status"):
            continue
        for sp in r.get("spikes", []):
            if not str(sp.get("kind", "")).startswith(("候補", "集積")):
                continue
            key = f"{r['code']}|{sp['date']}"
            prev = cands.get(key, {})
            cands[key] = {
                "code": r["code"], "name": r["name"], "layer": r["layer"],
                "spike_date": sp["date"],
                "spike_close": sp["close"], "spike_open": sp["open"],
                "spike_range": _f((sp["high"] or 0) - (sp["low"] or 0)),
                "crp": sp["crp"], "vol_ratio": sp["vol_ratio"],
                "earnings_spike": sp["earnings_spike"],
                "first_seen": prev.get("first_seen", today.isoformat()),
                "last_eval": today.isoformat(),
                "kind": sp["kind"],
                "followup_pass": (sp.get("followup") or {}).get("pass"),
            }
    # 期限切れの整理。営業日ベースで数える。
    keep = {}
    for k, c in cands.items():
        try:
            sd = dt.date.fromisoformat(c["spike_date"])
        except Exception:
            continue
        if int(np.busday_count(sd, today)) <= p.candidate_keep_days:
            keep[k] = c
    state["candidates"] = keep
    state["updated_at"] = dt.datetime.now(JST).isoformat(timespec="seconds")
    return state


def save_state(state: dict, out_dir: pathlib.Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def tracking_codes(state: dict) -> list[str]:
    """平日の追跡対象。層2で候補フラグが立っている銘柄のコード。"""
    return sorted({c["code"] for c in state.get("candidates", {}).values()
                   if c.get("layer") == "universe"})


# --------------------------------------------------------------------------
# 組み立て
# --------------------------------------------------------------------------
def core_targets(cfg: dict, p: Params) -> list[dict]:
    """層1の母集団。config.yaml の既存グループから作る（銘柄を直書きしない）。"""
    groups = p.layers["core"]["groups"]
    seen, out = set(), []
    for g in groups:
        for it in cfg.get(g) or []:
            if it["code"] in seen:
                continue
            seen.add(it["code"])
            out.append({"code": it["code"], "name": it["name"], "group": g})
    return out


def _needs_earnings(d: pd.DataFrame, p: Params) -> bool:
    """この銘柄の決算日をTDnetに照会する必要があるか。

    照会が要るのは「画面に出るスパイク」を持つ銘柄だけ。出来高が2倍でも終値位置が
    中間(0.3〜0.7)の日は候補にも逆アラートにもならないので、決算かどうかを問う意味が無い。
    層2(数百銘柄)ではこの1条件で照会数が減る（225銘柄で実測 174→141）。
    """
    s = max(p.vol_sma_days + 1, len(d) - p.scan_days)
    w = d.iloc[s:]
    m = w["vol_ratio"] >= p.vol_spike_ratio
    if not bool(m.any()):
        return False
    crp = w["crp"][m]
    return bool(((crp >= p.crp_high) | (crp <= p.crp_low)).any())


def _analyze_layer(targets: list[dict], frames: dict, p: Params, layer: str,
                   out_dir: pathlib.Path) -> list[dict]:
    """1つの層ぶんの判定。決算日の照会もここで層単位にまとめる。"""
    import earnings as earnings_mod

    prepared: dict[str, pd.DataFrame] = {}
    want_earn: list[str] = []
    need = {"Open", "High", "Low", "Close", "Volume"}
    for t in targets:
        df = frames.get(t["code"])
        if df is None or df.empty or len(df) < p.min_history_days:
            continue
        if not need.issubset(df.columns):
            continue
        d = prepare(df, p)
        prepared[t["code"]] = d
        if _needs_earnings(d, p):
            want_earn.append(t["code"])

    earn_map: dict[str, dict] = {}
    if want_earn:
        try:
            earn_map = earnings_mod.past_announcements(
                want_earn, out_dir=out_dir,
                cache_hours=p.earn_cache_hours, limit=p.earn_tdnet_limit)
        except Exception as e:
            # 決算日が取れなくても判定自体は続ける。ただし全スパイクが「判別不可」になる。
            earn_map = {c: {"status": f"取得失敗: {type(e).__name__}: {e}",
                            "announcements": None, "covers_from": None} for c in want_earn}
    # 照会しなかった銘柄は「取得失敗」ではなく「取りに行く必要が無かった」。
    # 同じ文言にすると、決算日が取れていない銘柄と見分けがつかなくなる。
    skipped = {"status": "判別対象のスパイクが無いため決算日は照会していない",
               "announcements": None, "covers_from": None}

    return [analyze(t["code"], t["name"], frames.get(t["code"]),
                    earn_map.get(t["code"], skipped), p, layer,
                    prepared=prepared.get(t["code"])) for t in targets]


def _universe_layer(cfg: dict, p: Params, out_dir: pathlib.Path, state: dict,
                    today: dt.date, full_scan: bool | None) -> tuple[dict, list[dict]]:
    """層2。週1回はユニバース全体、平日は候補フラグが立っている銘柄だけを追跡する。

    数百銘柄を毎朝 yfinance で叩くと失敗しやすいので二段構えにする（Eのとおり）。
    候補フラグは out/accumulation_state.json に永続化してあり、翌営業日以降も引き継げる。
    """
    import collect
    import universe as universe_mod

    ucfg = p.layers["universe"]
    meta = {"label": ucfg["label"], "note": ucfg.get("note"),
            "enabled": bool(ucfg.get("enabled")), "mode": ucfg.get("mode"),
            "filters": ucfg.get("filters"), "results": [], "n_targets": 0}

    if not ucfg.get("enabled"):
        # 「該当ゼロ」ではなく「未構築」。空配列だけで表現すると画面で区別がつかない。
        meta["status"] = "ユニバース未構築（config で enabled: false）"
        return meta, []

    # 土日はフル走査、平日は追跡のみ。明示指定があればそちらを優先する。
    if full_scan is None:
        full_scan = today.weekday() >= 5
    meta["scan_mode"] = "full" if full_scan else "tracking_only"

    if full_scan:
        u = universe_mod.members(cfg, out_dir)
        # フル走査日にユニバースが古い/未構築なら、その場で作り直す。
        # ここで作り直さないと、ユニバースのTTLが切れた週から層2が黙って止まり、
        # 画面は「未構築」を出し続ける（間違った値は出ないが、機能が死んでいることに
        # 気づくのが遅れる）。重い処理なのでフル走査日だけに限る。
        if (u.get("members") is None and ucfg.get("mode") == "jpx_filtered"
                and ucfg.get("auto_rebuild", True)):
            meta["rebuilt"] = True
            meta["rebuild_reason"] = u.get("status")
            try:
                universe_mod.build_jpx_universe(cfg, out_dir, verbose=False)
                u = universe_mod.members(cfg, out_dir)
            except Exception as e:
                u = {"status": f"ユニバースの再構築に失敗: {type(e).__name__}: {e}",
                     "members": None}
        if u.get("members") is None:
            # 取得できなければ層1のみで動作する（Cのとおり）。ゼロ件にはしない。
            meta["status"] = u.get("status") or "ユニバースを取得できていません"
            return meta, []
        targets = [{"code": m["code"], "name": m["name"]} for m in u["members"]]
        meta["source"] = u.get("source")
        meta["universe_as_of"] = u.get("as_of")
        # 日経の構成銘柄一覧は著作物なので、母集団の中身は公開JSONに出さない。
        meta["members_published"] = bool(u.get("publishable"))
    else:
        codes = tracking_codes(state)
        names = {c["code"]: c["name"] for c in state.get("candidates", {}).values()}
        targets = [{"code": c, "name": names.get(c, c)} for c in codes]
        meta["source"] = "out/accumulation_state.json の候補フラグ"
        if not targets:
            meta["status"] = None
            meta["n_targets"] = 0
            meta["note_today"] = "追跡中の候補なし（週1回のフル走査で候補が立つ）"
            return meta, []

    f = ucfg.get("fetch") or {}
    frames, failed = collect.fetch_ohlcv_batch(
        [t["code"] for t in targets], period=f.get("period", "3y"),
        chunk=f.get("chunk", 40), pause=f.get("pause_sec", 0.0))

    results = _analyze_layer(targets, frames, p, "universe", out_dir)
    meta["status"] = None
    meta["n_targets"] = len(targets)
    meta["n_fetched"] = len(frames)
    meta["fetch_failed"] = [{"code": c, "name": next((t["name"] for t in targets
                                                      if t["code"] == c), c), "error": why}
                            for c, why in failed.items()]
    # 層2は数百銘柄になるので results 全件はJSONに出さない（ファイルが肥大化し、
    # ダッシュボードの読み込みが重くなる）。シグナルになった行は下の集計に載る。
    meta["results"] = []
    meta["results_omitted"] = len(results)
    return meta, results


def build(cfg: dict, hist: dict, out_dir: pathlib.Path, fetch=None,
          today: dt.date | None = None, full_scan: bool | None = None) -> dict:
    """層1＋層2を判定して out/accumulation.json の中身を作る。

    hist: main.py が既に取得済みの {code: OHLCV DataFrame}。使い回して再取得を避ける。
    fetch: hist に無い銘柄を取りに行く関数（collect.fetch_history）。
    full_scan: 層2をフル走査するか。None なら土日だけフル走査（Eのとおり）。
    """
    p = Params(cfg)
    today = today or dt.datetime.now(JST).date()
    targets = core_targets(cfg, p)

    frames: dict[str, pd.DataFrame] = {}
    fetch_failed: list[dict] = []
    for t in targets:
        df = hist.get(t["code"])
        if (df is None or df.empty) and fetch is not None:
            try:
                df = fetch(t["code"])
            except Exception as e:
                fetch_failed.append({"code": t["code"], "name": t["name"],
                                     "error": f"{type(e).__name__}: {e}"})
                df = None
        frames[t["code"]] = df

    core_results = _analyze_layer(targets, frames, p, "core", out_dir)

    state = load_state(out_dir)
    uni_meta, uni_results = _universe_layer(cfg, p, out_dir, state, today, full_scan)

    all_results = core_results + uni_results
    state = update_state(state, all_results, p, today)
    save_state(state, out_dir)

    # 公開するシグナル行の上限。理由は2つ:
    #   1) 層2が数百銘柄になると1リストが100件超になり、ダッシュボードが読む
    #      accumulation.json が肥大化する（225銘柄の時点で92KB）。
    #   2) ユニバースの出典によっては、母集団の大部分を並べること自体が
    #      銘柄一覧の再配布に近づく（日経225の構成銘柄一覧は日経の著作物）。
    # 層1は保有・監視の13銘柄なので常に全件出す。切るのは層2だけ。
    max_rows = int((p.raw.get("publish") or {}).get("max_universe_rows", 20))
    truncated: dict[str, int] = {}

    def _collect(kind_prefix: str, key: str) -> list[dict]:
        core_rows, uni_rows = [], []
        for r in all_results:
            for sp in r.get("spikes", []):
                if str(sp.get("kind", "")).startswith(kind_prefix):
                    row = {"code": r["code"], "name": r["name"],
                           "layer": r["layer"], **sp}
                    (core_rows if r["layer"] == "core" else uni_rows).append(row)
        for rows in (core_rows, uni_rows):
            rows.sort(key=lambda x: (x["date"], x["code"]), reverse=True)
        kept_uni = uni_rows[:max_rows]
        # 「上限で切った」と「該当がそれだけだった」を区別できるよう件数を残す。
        truncated[key] = len(uni_rows) - len(kept_uni)
        return core_rows + kept_uni

    # 日数不足は層1だけ列挙する。層2は数百銘柄あり、上場が新しい銘柄が毎回大量に並ぶと
    # 「保有株の判定が落ちている」という重要な情報が埋もれる。層2は件数だけ持つ。
    insufficient = [{"code": r["code"], "name": r["name"], "layer": r["layer"],
                     "reason": r["status"], "history_days": r.get("history_days")}
                    for r in core_results if r.get("status")]
    uni_meta["n_insufficient"] = sum(1 for r in uni_results if r.get("status"))

    return {
        "as_of": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "params": p.as_dict(),
        "method": "① V/SMA(V,20)>=閾値 ② CRP=(C-L)/(H-L) ③ 50日U/D出来高比 "
                  "④ スパイク後10日の押し込み耐性・窓維持・レンジ収縮",
        "layers": {
            "core": {
                "label": p.layers["core"]["label"],
                "note": p.layers["core"].get("note"),
                "enabled": True,
                "status": None,
                "n_targets": len(targets),
                "fetch_failed": fetch_failed,
                "results": core_results,
            },
            "universe": uni_meta,
        },
        "accumulation": _collect("集積", "accumulation"),
        "distribution": _collect("売り抜け", "distribution"),
        "tracking": _collect("候補（追跡中）", "tracking"),
        # 層2で上限を超えて出さなかった件数。0 と「そもそも該当ゼロ」は別物。
        "universe_rows_omitted": truncated,
        "max_universe_rows": max_rows,
        "insufficient": insufficient,
        "candidates_tracked": len(state.get("candidates", {})),
    }


def write(cfg: dict, hist: dict, out_dir: pathlib.Path, fetch=None,
          full_scan: bool | None = None) -> dict:
    data = build(cfg, hist, out_dir, fetch=fetch, full_scan=full_scan)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "accumulation.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return data


if __name__ == "__main__":
    import sys
    import yaml

    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import collect  # noqa: E402

    ROOT = pathlib.Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    # --full / --tracking で層2の走査モードを明示できる（既定は土日だけフル走査）
    fs = True if "--full" in sys.argv else False if "--tracking" in sys.argv else None
    data = write(cfg, {}, ROOT / "out", fetch=collect.fetch_history, full_scan=fs)
    u = data["layers"]["universe"]
    print(f"層1 targets={data['layers']['core']['n_targets']} / "
          f"層2 {u.get('scan_mode') or '-'} targets={u.get('n_targets', 0)} "
          f"status={u.get('status')}")
    print(f"accumulation={len(data['accumulation'])} "
          f"distribution={len(data['distribution'])} "
          f"tracking={len(data['tracking'])} "
          f"insufficient(層1)={len(data['insufficient'])} "
          f"candidates={data['candidates_tracked']}")
    for r in data["insufficient"]:
        print(f"  除外 {r['code']} {r['name']}: {r['reason']}")
