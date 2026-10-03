/*
 * BOAT RACE SNIPER 学習モデル（ブラウザ用）
 *
 * pipeline/analyze.py が毎週書き出す weights.json を読み込んで、
 *   ・各艇の1着確率
 *   ・3連単120通りの確率
 *   ・荒れ指数（万舟になる確率）
 *   ・学習で一番回収率が良かった買い目
 * を返す。
 *
 * 使い方（index.html）:
 *   <script src="/model/sniper-model.js"></script>
 *   const model = await SniperModel.load();          // weights.json を取得
 *   const r = model.predict({
 *     venue: "12",            // 場コード（住之江=12）
 *     windSpeed: 3, wave: 3,  // 風速m・波高cm（分からなければ0）
 *     boats: [                // 1号艇〜6号艇の順
 *       { cls: "A1", motor2: 38.2, boat2: 33.1, natWin: 7.2, locWin: 6.9, exTime: 6.75, racerNo: 4444 },
 *       ...
 *     ],
 *   });
 *   r.winProbs   // [0.52, 0.14, ...]
 *   r.arare      // 0.31 → 31% で万舟
 *   r.picks      // [{ combo: "1-4-2", prob: 0.012 }, ...]
 *
 * 勝率・展示タイムなど分からない値は省略してOK（学習データの平均で埋める）。
 */
(function (root) {
  "use strict";

  const COMBOS = [];
  for (let i = 0; i < 6; i++)
    for (let j = 0; j < 6; j++)
      for (let k = 0; k < 6; k++)
        if (i !== j && j !== k && i !== k) COMBOS.push([i, j, k]);

  const num = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? null : Number(v));
  const logit = (p) => {
    const q = Math.min(Math.max(p, 1e-4), 1 - 1e-4);
    return Math.log(q / (1 - q));
  };

  function raceDiff(values) {
    const known = values.filter((v) => v !== null);
    if (!known.length) return values.map(() => 0);
    const mean = known.reduce((a, b) => a + b, 0) / known.length;
    return values.map((v) => (v === null ? 0 : v - mean));
  }

  function softmax(z) {
    const m = Math.max(...z);
    const e = z.map((v) => Math.exp(v - m));
    const s = e.reduce((a, b) => a + b, 0);
    return e.map((v) => v / s);
  }

  function harville(p) {
    const out = COMBOS.map(([i, j, k]) => {
      const a = Math.max(1 - p[i], 1e-9);
      const b = Math.max(1 - p[i] - p[j], 1e-9);
      return p[i] * (p[j] / a) * (p[k] / b);
    });
    const s = out.reduce((x, y) => x + y, 0);
    return out.map((v) => v / s);
  }

  class SniperModel {
    constructor(weights, racerSt) {
      this.w = weights;
      this.racerSt = racerSt || {};
    }

    static async load(base = "/model/") {
      const [w, st] = await Promise.all([
        fetch(base + "weights.json", { cache: "no-cache" }).then((r) => r.json()),
        fetch(base + "racer_st.json", { cache: "no-cache" }).then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
      ]);
      return new SniperModel(w, st);
    }

    features(input) {
      const W = this.w;
      const boats = input.boats;
      if (!boats || boats.length !== 6) throw new Error("boats は6艇分必要です");
      const wind = num(input.windSpeed) ?? 0;
      const wave = num(input.wave) ?? 0;
      const v = W.venues[String(input.venue).padStart(2, "0")];
      const vIn = logit(v ? v.in_win : W.mean_in_win) - logit(W.mean_in_win);

      const nat = boats.map((b) => num(b.natWin) ?? W.fill.nat_win);
      const loc = boats.map((b, i) => {
        const x = num(b.locWin);
        return x === null || x <= 0 ? nat[i] : x;
      });
      const ex = raceDiff(boats.map((b) => num(b.exTime)));
      const st = raceDiff(
        boats.map((b) => {
          const own = num(b.stAvg);
          if (own !== null) return own;
          const rec = b.racerNo ? this.racerSt[String(b.racerNo)] : null;
          return rec ? rec[0] : null;
        })
      );

      return boats.map((b, i) => {
        const lane = i + 1;
        const f = {
          lane2: lane === 2 ? 1 : 0,
          lane3: lane === 3 ? 1 : 0,
          lane4: lane === 4 ? 1 : 0,
          lane5: lane === 5 ? 1 : 0,
          lane6: lane === 6 ? 1 : 0,
          cls: W.cls_score[b.cls] ?? 1,
          nat_win: nat[i],
          loc_win: loc[i],
          motor_2: (num(b.motor2) ?? W.fill.motor_2) / 10,
          boat_2: (num(b.boat2) ?? W.fill.boat_2) / 10,
          ex_diff: ex[i] * 10,
          st_diff: st[i] * 10,
          in_x_venue: lane === 1 ? vIn : 0,
          in_x_wind: lane === 1 ? wind : 0,
          in_x_wave: lane === 1 ? wave / 5 : 0,
          out_x_wind: lane >= 4 ? wind : 0,
        };
        return W.win_model.features.map((name) => f[name] ?? 0);
      });
    }

    predict(input) {
      const W = this.w;
      const X = this.features(input);
      const coef = W.win_model.coef;
      const winProbs = softmax(X.map((row) => row.reduce((s, x, j) => s + x * coef[j], 0)));
      const tri = harville(winProbs);

      // 荒れ指数
      const v = W.venues[String(input.venue).padStart(2, "0")];
      const entropy = -winProbs.reduce((s, p) => s + p * Math.log(p + 1e-12), 0);
      const top3 = [...tri].sort((a, b) => b - a).slice(0, 3).reduce((a, b) => a + b, 0);
      const A = {
        p_in: winProbs[0],
        p_top: Math.max(...winProbs),
        entropy,
        top3_share: top3,
        wind_speed: (num(input.windSpeed) ?? 0) / 5,
        wave: (num(input.wave) ?? 0) / 5,
        venue_manshu: logit(v ? v.manshu : W.mean_manshu),
      };
      const am = W.arare_model;
      let z = am.coef[0];
      am.features.forEach((name, j) => {
        z += am.coef[j + 1] * ((A[name] - am.mean[j]) / am.std[j]);
      });
      const arare = 1 / (1 + Math.exp(-z));

      const ranked = COMBOS.map((c, idx) => ({ combo: c.map((x) => x + 1).join("-"), prob: tri[idx] })).sort(
        (a, b) => b.prob - a.prob
      );
      const s = W.strategy;
      const picks = s ? ranked.slice(s.skip, s.skip + s.buy) : ranked.slice(0, 10);
      const shouldBet = s ? arare >= s.arare_threshold : true;

      return {
        winProbs,
        arare,
        shouldBet,
        honmei: ranked.slice(0, 5),
        picks,
        ranked,
        fairOdds: (prob) => (prob > 0 ? 0.75 / prob : Infinity), // 控除率25%を引いた理論オッズ
        meta: { trainedAt: W.trained_at, nRaces: W.n_races, metrics: W.metrics },
      };
    }
  }

  if (typeof module !== "undefined" && module.exports) module.exports = SniperModel;
  else root.SniperModel = SniperModel;
})(typeof window !== "undefined" ? window : globalThis);
