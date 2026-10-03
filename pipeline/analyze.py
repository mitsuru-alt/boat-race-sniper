"""溜まったデータから予想モデルを学習し、回収率を検証して weights.json を書き出す。

    python pipeline/analyze.py

入力 : data/entries/**/*.csv.gz
出力 : model/weights.json   … アプリが読む学習済みの重み
       model/racer_st.json  … 選手ごとの平均スタートタイミング
       reports/latest.md           … 日本語の成績レポート
       reports/history.csv         … 学習ごとの成績推移

モデルは2段構え:
  1. 勝率モデル（条件付きロジット）… 6艇のうち誰が1着かの確率
     → ハービル式で3連単120通りの確率に展開
  2. 荒れモデル（ロジスティック回帰）… 3連単が万舟（10,000円以上）になる確率 = 荒れ指数

評価は時系列で「学習 → 検証 → テスト」に分け、テスト期間は学習に一切使わない。
"""

from __future__ import annotations

import json
import math
import sys
import warnings
from datetime import datetime, timedelta, timezone
from itertools import permutations
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "entries"
MODEL_DIR = ROOT / "model"
REPORT_DIR = ROOT / "reports"
JST = timezone(timedelta(hours=9))

VENUE_NAMES = {
    "01": "桐生", "02": "戸田", "03": "江戸川", "04": "平和島", "05": "多摩川",
    "06": "浜名湖", "07": "蒲郡", "08": "常滑", "09": "津", "10": "三国",
    "11": "びわこ", "12": "住之江", "13": "尼崎", "14": "鳴門", "15": "丸亀",
    "16": "児島", "17": "宮島", "18": "徳山", "19": "下関", "20": "若松",
    "21": "芦屋", "22": "福岡", "23": "唐津", "24": "大村",
}
CLS_SCORE = {"A1": 3.0, "A2": 2.0, "B1": 1.0, "B2": 0.0}
MANSHU = 10_000  # 万舟の基準（円）
L2 = 1e-3

# 勝率モデルの特徴量（順番は weights.json と JS で共有）
WIN_FEATURES = [
    "lane2", "lane3", "lane4", "lane5", "lane6",
    "cls", "nat_win", "loc_win", "motor_2", "boat_2",
    "ex_diff", "st_diff",
    "in_x_venue", "in_x_wind", "in_x_wave", "out_x_wind",
]
# 荒れモデルの特徴量
ARARE_FEATURES = ["p_in", "p_top", "entropy", "top3_share", "wind_speed", "wave", "venue_manshu"]

COMBOS = list(permutations(range(6), 3))  # 120通り（0始まり）


# ----------------------------------------------------------------------
# データ読み込み
# ----------------------------------------------------------------------

def load_entries() -> pd.DataFrame:
    files = sorted(DATA_DIR.glob("*/*.csv.gz"))
    if not files:
        return pd.DataFrame()
    df = pd.concat((pd.read_csv(f, dtype={"venue": str, "tri_combo": str}) for f in files), ignore_index=True)
    df["venue"] = df["venue"].str.zfill(2)
    return df


def add_racer_st(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """選手の『その日より前』の平均STを付ける（未来の情報を混ぜない）。"""
    st = df.dropna(subset=["st", "racer_no"])
    st = st[(st["st"] > -0.2) & (st["st"] < 1.0)]
    daily = st.groupby(["racer_no", "date"])["st"].agg(["sum", "count"]).reset_index().sort_values(["racer_no", "date"])
    daily["cum_sum"] = daily.groupby("racer_no")["sum"].cumsum() - daily["sum"]
    daily["cum_cnt"] = daily.groupby("racer_no")["count"].cumsum() - daily["count"]
    daily["st_avg_prior"] = np.where(daily["cum_cnt"] >= 5, daily["cum_sum"] / daily["cum_cnt"].clip(lower=1), np.nan)
    df = df.merge(daily[["racer_no", "date", "st_avg_prior"]], on=["racer_no", "date"], how="left")

    # アプリ用：全期間の最新平均ST
    latest = st.groupby("racer_no")["st"].agg(["mean", "count"])
    latest = latest[latest["count"] >= 5]
    racer_st = {str(int(k)): [round(float(v["mean"]), 3), int(v["count"])] for k, v in latest.iterrows()}
    return df, racer_st


def build_races(df: pd.DataFrame) -> pd.DataFrame:
    """6艇そろって結果があるレースだけ残し、レース単位に並べ替える。"""
    df = df.copy()
    df["race_id"] = df["date"] + "_" + df["venue"] + "_" + df["race_no"].astype(str).str.zfill(2)
    ok = df.groupby("race_id").agg(n=("lane", "nunique"), winners=("finish", lambda s: (s == 1).sum()),
                                   has_b=("nat_win", lambda s: s.notna().sum()))
    keep = ok[(ok["n"] == 6) & (ok["winners"] == 1) & (ok["has_b"] == 6)].index
    df = df[df["race_id"].isin(keep)].sort_values(["date", "race_id", "lane"]).reset_index(drop=True)
    return df


# ----------------------------------------------------------------------
# 特徴量
# ----------------------------------------------------------------------

def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def venue_stats(df: pd.DataFrame) -> dict:
    races = df.drop_duplicates("race_id")
    lane1 = df[df["lane"] == 1]
    out = {}
    for v in sorted(df["venue"].unique()):
        l1 = lane1[lane1["venue"] == v]
        rv = races[races["venue"] == v]
        rv = rv[rv["tri_payout"].notna()]
        out[v] = {
            "name": VENUE_NAMES.get(v, v),
            "in_win": float((l1["finish"] == 1).mean()) if len(l1) else 0.55,
            "manshu": float((rv["tri_payout"] >= MANSHU).mean()) if len(rv) else 0.15,
            "races": int(len(l1)),
        }
    return out


def feature_tensor(df: pd.DataFrame, vstats: dict, mean_in: float, mean_manshu: float) -> np.ndarray:
    """(レース数, 6, 特徴量数) の配列を作る。"""
    n = len(df) // 6
    lane = df["lane"].to_numpy().reshape(n, 6)
    cls = df["cls"].map(CLS_SCORE).fillna(1.0).to_numpy().reshape(n, 6)
    nat = df["nat_win"].fillna(df["nat_win"].mean()).to_numpy().reshape(n, 6)
    loc = df["loc_win"].to_numpy(dtype=float).reshape(n, 6)
    loc = np.where(np.isnan(loc) | (loc <= 0), nat, loc)  # 当地実績なし → 全国勝率で代用
    mot = df["motor_2"].fillna(df["motor_2"].mean()).to_numpy().reshape(n, 6)
    boa = df["boat_2"].fillna(df["boat_2"].mean()).to_numpy().reshape(n, 6)
    ex = df["ex_time"].to_numpy(dtype=float).reshape(n, 6)
    st = df["st_avg_prior"].to_numpy(dtype=float).reshape(n, 6)
    wind = df["wind_speed"].fillna(0).to_numpy().reshape(n, 6)[:, 0]
    wave = df["wave"].fillna(0).to_numpy().reshape(n, 6)[:, 0]
    venue = df["venue"].to_numpy().reshape(n, 6)[:, 0]
    v_in = np.array([logit(vstats.get(v, {}).get("in_win", mean_in)) - logit(mean_in) for v in venue])

    X = np.zeros((n, 6, len(WIN_FEATURES)))
    for k in range(2, 7):
        X[:, :, WIN_FEATURES.index(f"lane{k}")] = (lane == k)
    X[:, :, WIN_FEATURES.index("cls")] = cls
    X[:, :, WIN_FEATURES.index("nat_win")] = nat
    X[:, :, WIN_FEATURES.index("loc_win")] = loc
    X[:, :, WIN_FEATURES.index("motor_2")] = mot / 10.0
    X[:, :, WIN_FEATURES.index("boat_2")] = boa / 10.0
    X[:, :, WIN_FEATURES.index("ex_diff")] = race_diff(ex) * 10.0   # 0.1秒単位
    X[:, :, WIN_FEATURES.index("st_diff")] = race_diff(st) * 10.0
    is_in = (lane == 1).astype(float)
    is_out = (lane >= 4).astype(float)
    X[:, :, WIN_FEATURES.index("in_x_venue")] = is_in * v_in[:, None]
    X[:, :, WIN_FEATURES.index("in_x_wind")] = is_in * wind[:, None]
    X[:, :, WIN_FEATURES.index("in_x_wave")] = is_in * wave[:, None] / 5.0
    X[:, :, WIN_FEATURES.index("out_x_wind")] = is_out * wind[:, None]
    return X


def race_diff(a: np.ndarray) -> np.ndarray:
    """レース平均との差。欠けている艇は0（平均扱い）。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)  # 全艇欠けたレースは平均なし
        m = np.nanmean(a, axis=1, keepdims=True)
    d = a - m
    return np.nan_to_num(d, nan=0.0)


# ----------------------------------------------------------------------
# 勝率モデル（条件付きロジット）
# ----------------------------------------------------------------------

def softmax_rows(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def fit_win_model(X: np.ndarray, y: np.ndarray, feat_mask: np.ndarray | None = None) -> np.ndarray:
    n, _, f = X.shape
    mask = np.ones(f) if feat_mask is None else feat_mask

    def loss_grad(w):
        w = w * mask
        p = softmax_rows(X @ w)
        ll = -np.log(p[np.arange(n), y] + 1e-12).sum() / n + L2 * (w @ w)
        onehot = np.zeros_like(p)
        onehot[np.arange(n), y] = 1
        g = np.einsum("ij,ijf->f", p - onehot, X) / n + 2 * L2 * w
        return ll, g * mask

    res = minimize(loss_grad, np.zeros(f), jac=True, method="L-BFGS-B", options={"maxiter": 500})
    return res.x * mask


def harville(p: np.ndarray) -> np.ndarray:
    """1着確率(レース数,6) → 3連単120通りの確率(レース数,120)。"""
    out = np.zeros((p.shape[0], len(COMBOS)))
    for c, (i, j, k) in enumerate(COMBOS):
        pi, pj, pk = p[:, i], p[:, j], p[:, k]
        out[:, c] = pi * pj / np.clip(1 - pi, 1e-9, None) * pk / np.clip(1 - pi - pj, 1e-9, None)
    return out / out.sum(axis=1, keepdims=True)


# ----------------------------------------------------------------------
# 荒れモデル（ロジスティック回帰）
# ----------------------------------------------------------------------

def arare_features(p: np.ndarray, tri: np.ndarray, races: pd.DataFrame, vstats: dict, mean_manshu: float) -> np.ndarray:
    ent = -(p * np.log(p + 1e-12)).sum(axis=1)
    top3 = np.sort(tri, axis=1)[:, -3:].sum(axis=1)
    venue_m = np.array([logit(vstats.get(v, {}).get("manshu", mean_manshu)) for v in races["venue"]])
    A = np.column_stack([
        p[:, 0],
        p.max(axis=1),
        ent,
        top3,
        races["wind_speed"].fillna(0).to_numpy() / 5.0,
        races["wave"].fillna(0).to_numpy() / 5.0,
        venue_m,
    ])
    return A


def fit_logistic(A: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float]:
    mu, sd = A.mean(axis=0), A.std(axis=0) + 1e-9
    Z = (A - mu) / sd
    Z1 = np.column_stack([np.ones(len(Z)), Z])

    def loss_grad(w):
        z = Z1 @ w
        q = 1 / (1 + np.exp(-z))
        ll = -(y * np.log(q + 1e-12) + (1 - y) * np.log(1 - q + 1e-12)).mean() + L2 * (w[1:] @ w[1:])
        g = Z1.T @ (q - y) / len(y)
        g[1:] += 2 * L2 * w[1:]
        return ll, g

    res = minimize(loss_grad, np.zeros(Z1.shape[1]), jac=True, method="L-BFGS-B")
    return res.x, (mu, sd)


def predict_logistic(A, w, norm):
    mu, sd = norm
    z = np.column_stack([np.ones(len(A)), (A - mu) / sd]) @ w
    return 1 / (1 + np.exp(-z))


# ----------------------------------------------------------------------
# 回収率シミュレーション
# ----------------------------------------------------------------------

def combo_index(races: pd.DataFrame) -> np.ndarray:
    lut = {f"{i+1}-{j+1}-{k+1}": c for c, (i, j, k) in enumerate(COMBOS)}
    return np.array([lut.get(str(c), -1) for c in races["tri_combo"]])


def simulate(tri, arare, hit_idx, payout, threshold, skip, buy):
    """荒れ指数 >= threshold のレースだけ、確率順位 skip+1〜skip+buy 位の3連単を各100円買う。"""
    sel = arare >= threshold
    if sel.sum() == 0:
        return {"races": 0, "bets": 0, "hits": 0, "cost": 0, "ret": 0, "roi": 0.0}
    order = np.argsort(-tri[sel], axis=1)[:, skip:skip + buy]
    hit = (order == hit_idx[sel][:, None]).any(axis=1)
    cost = int(sel.sum()) * buy * 100
    ret = float((payout[sel] * hit).sum())
    return {"races": int(sel.sum()), "bets": int(sel.sum()) * buy, "hits": int(hit.sum()),
            "cost": cost, "ret": int(ret), "roi": ret / cost if cost else 0.0}


def search_strategy(tri, arare, hit_idx, payout):
    best = None
    for q in (0.0, 0.5, 0.7, 0.8, 0.9):
        th = float(np.quantile(arare, q)) if q > 0 else 0.0
        for skip in (0, 3, 6, 10, 20):
            for buy in (5, 10, 20):
                r = simulate(tri, arare, hit_idx, payout, th, skip, buy)
                if r["races"] < 30:  # 少なすぎる条件はまぐれなので除外
                    continue
                score = r["roi"]
                if best is None or score > best[0]:
                    best = (score, {"quantile": q, "threshold": th, "skip": skip, "buy": buy, **r})
    return best[1] if best else None


# ----------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------

def split_dates(dates: np.ndarray):
    uniq = np.sort(np.unique(dates))
    n = len(uniq)
    test_from = uniq[int(n * 0.85)] if n >= 20 else uniq[-1]
    val_from = uniq[int(n * 0.70)] if n >= 20 else uniq[-1]
    return val_from, test_from


def main() -> int:
    raw = load_entries()
    if raw.empty:
        print("データがまだありません。先に collect.py を実行してください。")
        return 1
    raw, racer_st = add_racer_st(raw)
    df = build_races(raw)
    n_races = len(df) // 6
    print(f"学習に使えるレース: {n_races:,}  （期間 {df['date'].min()} 〜 {df['date'].max()}）")
    if n_races < 300:
        print("レース数が少なすぎるので学習をスキップします（最低300レース）。")
        return 0

    races = df.groupby("race_id", sort=False).first().reset_index()
    y = df.loc[df["finish"] == 1, "lane"].to_numpy() - 1
    val_from, test_from = split_dates(races["date"].to_numpy())
    tr = (races["date"] < val_from).to_numpy()
    va = ((races["date"] >= val_from) & (races["date"] < test_from)).to_numpy()
    te = (races["date"] >= test_from).to_numpy()
    fit_mask = tr | va

    # 場ごとの傾向は学習期間だけから計算
    vstats = venue_stats(df[df["race_id"].isin(races.loc[fit_mask, "race_id"])])
    mean_in = float(np.mean([v["in_win"] for v in vstats.values()]))
    mean_manshu = float(np.mean([v["manshu"] for v in vstats.values()]))
    X = feature_tensor(df, vstats, mean_in, mean_manshu)

    # --- 勝率モデル ---
    w_tr = fit_win_model(X[tr], y[tr])
    lane_mask = np.array([1.0 if f.startswith("lane") else 0.0 for f in WIN_FEATURES])
    w_lane = fit_win_model(X[tr], y[tr], lane_mask)  # 比較用：枠番だけのモデル

    def eval_win(w, m):
        p = softmax_rows(X[m] @ w)
        return {
            "top1_acc": float((p.argmax(axis=1) == y[m]).mean()),
            "logloss": float(-np.log(p[np.arange(m.sum()), y[m]] + 1e-12).mean()),
        }

    win_eval = {"model": eval_win(w_tr, te), "lane_only": eval_win(w_lane, te),
                "always_1": float((y[te] == 0).mean())}

    # --- 荒れモデル（学習期間で学習、検証で戦略選択、テストで評価）---
    p_all = softmax_rows(X @ w_tr)
    tri_all = harville(p_all)
    A_all = arare_features(p_all, tri_all, races, vstats, mean_manshu)
    payout = races["tri_payout"].fillna(0).to_numpy(dtype=float)
    hit_idx = combo_index(races)
    has_pay = (payout > 0) & (hit_idx >= 0)
    manshu = (payout >= MANSHU).astype(float)

    m_tr, m_va, m_te = tr & has_pay, va & has_pay, te & has_pay
    w_ar, ar_norm = fit_logistic(A_all[m_tr], manshu[m_tr])
    arare_all = predict_logistic(A_all, w_ar, ar_norm)

    def auc(score, label):
        order = np.argsort(score)
        ranks = np.empty(len(score)); ranks[order] = np.arange(1, len(score) + 1)
        pos = label == 1
        if pos.sum() == 0 or (~pos).sum() == 0:
            return float("nan")
        return float((ranks[pos].sum() - pos.sum() * (pos.sum() + 1) / 2) / (pos.sum() * (~pos).sum()))

    arare_eval = {"auc_test": auc(arare_all[m_te], manshu[m_te]), "manshu_rate_test": float(manshu[m_te].mean())}

    strategy = search_strategy(tri_all[m_va], arare_all[m_va], hit_idx[m_va], payout[m_va])
    test_result = None
    baselines = {}
    if strategy:
        test_result = simulate(tri_all[m_te], arare_all[m_te], hit_idx[m_te], payout[m_te],
                               strategy["threshold"], strategy["skip"], strategy["buy"])
    for skip, buy in ((0, 5), (0, 20)):
        baselines[f"全レース 上位{skip+1}〜{skip+buy}位"] = simulate(
            tri_all[m_te], np.ones(m_te.sum()), hit_idx[m_te], payout[m_te], 0.0, skip, buy)

    # --- 本番用：学習+検証期間で取り直す（テストは評価専用のまま）---
    w_final = fit_win_model(X[fit_mask], y[fit_mask])
    p_f = softmax_rows(X @ w_final)
    A_f = arare_features(p_f, harville(p_f), races, vstats, mean_manshu)
    mf = fit_mask & has_pay
    w_ar_f, ar_norm_f = fit_logistic(A_f[mf], manshu[mf])
    final_threshold = None
    if strategy:
        final_threshold = float(np.quantile(predict_logistic(A_f[mf], w_ar_f, ar_norm_f), strategy["quantile"])) if strategy["quantile"] > 0 else 0.0

    # 欠けた値を埋めるための平均（アプリで勝率が取れない時用）
    fill = {c: float(df[c].mean()) for c in ("nat_win", "loc_win", "motor_2", "boat_2")}

    now = datetime.now(JST)
    weights = {
        "version": 1,
        "trained_at": now.isoformat(timespec="seconds"),
        "data_from": str(df["date"].min()),
        "data_to": str(df["date"].max()),
        "n_races": n_races,
        "win_model": {"features": WIN_FEATURES, "coef": [round(float(v), 6) for v in w_final]},
        "arare_model": {
            "features": ARARE_FEATURES,
            "coef": [round(float(v), 6) for v in w_ar_f],
            "mean": [round(float(v), 6) for v in ar_norm_f[0]],
            "std": [round(float(v), 6) for v in ar_norm_f[1]],
        },
        "venues": {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in vstats.items()},
        "mean_in_win": round(mean_in, 4),
        "mean_manshu": round(mean_manshu, 4),
        "fill": {k: round(v, 3) for k, v in fill.items()},
        "cls_score": CLS_SCORE,
        "strategy": None if not strategy else {
            "arare_threshold": round(final_threshold, 4),
            "skip": strategy["skip"],
            "buy": strategy["buy"],
        },
        "metrics": {"win": win_eval, "arare": arare_eval, "strategy_test": test_result, "baselines_test": baselines,
                    "test_from": str(test_from)},
    }

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    (MODEL_DIR / "weights.json").write_text(json.dumps(weights, ensure_ascii=False, indent=1), encoding="utf-8")
    (MODEL_DIR / "racer_st.json").write_text(json.dumps(racer_st, separators=(",", ":")), encoding="utf-8")

    write_report(weights, strategy, test_result, baselines, vstats, now)
    print((REPORT_DIR / "latest.md").read_text(encoding="utf-8"))
    return 0


def write_report(weights, strategy, test_result, baselines, vstats, now):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    m = weights["metrics"]
    w = m["win"]
    lines = [
        f"# BOAT RACE SNIPER 学習レポート（{now:%Y-%m-%d %H:%M}）",
        "",
        f"- 学習データ: {weights['n_races']:,}レース（{weights['data_from']} 〜 {weights['data_to']}）",
        f"- テスト期間: {m['test_from']} 以降（学習には未使用）",
        "",
        "## 1着予想の精度（テスト期間）",
        "",
        "| モデル | 1着的中率 | ログ損失（低いほど良い） |",
        "|---|---|---|",
        f"| 今回のモデル | {w['model']['top1_acc']:.1%} | {w['model']['logloss']:.3f} |",
        f"| 枠番だけ | {w['lane_only']['top1_acc']:.1%} | {w['lane_only']['logloss']:.3f} |",
        f"| いつも1号艇 | {w['always_1']:.1%} | - |",
        "",
        "## 荒れ指数（万舟を見抜けるか）",
        "",
        f"- テスト期間の万舟率: {m['arare']['manshu_rate_test']:.1%}",
        f"- 判別力 AUC: {m['arare']['auc_test']:.3f}（0.5 = 当てずっぽう、0.7以上なら実用的）",
        "",
        "## 大穴戦略の回収率（テスト期間・3連単各100円）",
        "",
    ]
    if strategy and test_result:
        lines += [
            f"検証期間で一番良かった買い方: 荒れ指数 上位{(1-strategy['quantile']):.0%}のレースだけ、"
            f"確率{strategy['skip']+1}〜{strategy['skip']+strategy['buy']}位の3連単を{strategy['buy']}点",
            "",
            "| 買い方 | レース数 | 的中 | 投資 | 払戻 | 回収率 |",
            "|---|---|---|---|---|---|",
            _row("大穴戦略", test_result),
        ]
        for name, r in baselines.items():
            lines.append(_row(name, r))
        lines += [
            "",
            "※ 回収率100%未満は「買うほど減る」状態です。検証期間で良くても、テスト期間で下がるのは普通です。",
        ]
    else:
        lines.append("データ不足のため戦略検証はまだできません。")

    lines += ["", "## 場ごとの傾向（学習期間）", "", "| 場 | 1号艇1着率 | 万舟率 | レース数 |", "|---|---|---|---|"]
    for code, v in sorted(vstats.items(), key=lambda kv: kv[1]["manshu"], reverse=True):
        lines.append(f"| {v['name']} | {v['in_win']:.1%} | {v['manshu']:.1%} | {v['races']:,} |")

    coef = dict(zip(weights["win_model"]["features"], weights["win_model"]["coef"]))
    lines += ["", "## 効いている要素（勝率モデルの重み）", "", "| 要素 | 重み |", "|---|---|"]
    for k, v in sorted(coef.items(), key=lambda kv: -abs(kv[1])):
        lines.append(f"| {FEATURE_LABELS.get(k, k)} | {v:+.3f} |")

    (REPORT_DIR / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    hist = REPORT_DIR / "history.csv"
    new = not hist.exists()
    with hist.open("a", encoding="utf-8") as f:
        if new:
            f.write("trained_at,n_races,top1_acc,arare_auc,strategy_roi,strategy_races\n")
        roi = test_result["roi"] if test_result else ""
        nr = test_result["races"] if test_result else ""
        f.write(f"{weights['trained_at']},{weights['n_races']},{w['model']['top1_acc']:.4f},"
                f"{m['arare']['auc_test']:.4f},{roi},{nr}\n")


def _row(name, r):
    return f"| {name} | {r['races']:,} | {r['hits']:,} | ¥{r['cost']:,} | ¥{r['ret']:,} | {r['roi']:.1%} |"


FEATURE_LABELS = {
    "lane2": "2号艇", "lane3": "3号艇", "lane4": "4号艇", "lane5": "5号艇", "lane6": "6号艇",
    "cls": "級別", "nat_win": "全国勝率", "loc_win": "当地勝率", "motor_2": "モーター2連率",
    "boat_2": "ボート2連率", "ex_diff": "展示タイム（平均との差・遅いほど＋）",
    "st_diff": "平均ST（平均との差・遅いほど＋）", "in_x_venue": "イン×場のイン強さ",
    "in_x_wind": "イン×風速", "in_x_wave": "イン×波高", "out_x_wind": "外枠×風速",
}

if __name__ == "__main__":
    sys.exit(main())
