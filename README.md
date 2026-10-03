# BOAT RACE SNIPER 🎯

競艇の大穴予想ツール。公式の番組表・競走成績を毎日自動で集めて、予想モデルを週1で学び直します。

## 仕組み

| いつ | 何をする | ファイル |
|---|---|---|
| 毎晩 23:40 | 昨日・一昨日の番組表と結果を取得 | `.github/workflows/collect.yml` |
| 毎週月曜 4:10 | 溜まったデータで再学習・回収率を検証 | `.github/workflows/train.yml` |
| 手動 | 過去データを一括取得（初回に1回） | `.github/workflows/backfill.yml` |

- データ: `data/entries/年/YYYYMMDD.csv.gz`（1行 = 1レースの1艇）
- 学習結果: `model/weights.json`、選手の平均ST: `model/racer_st.json`
- 成績レポート: `reports/latest.md`、推移: `reports/history.csv`

データ元は BOAT RACE オフィシャルウェブサイトの「ダウンロード」で配布されている番組表（B）と競走成績（K）。

## 初回セットアップ

1. GitHub の **Actions** タブ →「過去データの一括取得」→ **Run workflow**（開始日は2年前くらいがおすすめ）
2. 終わると `reports/latest.md` に最初の成績が出ます
3. あとは毎晩・毎週、勝手に動きます

## アプリから使う

```html
<script src="/model/sniper-model.js"></script>
<script>
  const model = await SniperModel.load("/model/");
  const r = model.predict({ venue: "12", windSpeed: 3, wave: 3, boats: [/* 1〜6号艇 */] });
  // r.winProbs（1着確率）, r.arare（荒れ指数）, r.shouldBet, r.picks（買い目）
</script>
```

## 注意

競艇は控除率が約25%あるので、評価は的中率より**回収率**で見ます。レポートのテスト期間は学習に使っていない期間なので、ここで100%を超え続けるかが本当の実力です。
