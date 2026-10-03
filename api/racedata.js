// Jina AI reader (r.jina.ai) 経由でboatrace.jpを取得
// → Vercel/AWS IPブロックを回避できる
export const config = { runtime: 'edge' };

// Jina AI が返す Markdown からレーサーデータを解析
function parseJinaMarkdown(text) {
  const racers = [];

  for (const line of text.split('\n')) {
    if (racers.length >= 6) break;

    // 級別マーカー "/ A1" "/ B2" 等を含む行だけ処理
    const classMatch = line.match(/\/\s*(A[12]|B[12])/);
    if (!classMatch) continue;

    // パイプで区切ってセクションに分割
    const sections = line.split('|').map(s => s.trim());

    // モーター・ボートセクションのパターン: "整数 XX.XX XX.XX"
    // 例: "61 33.33 66.67" → モーター番号61、2連率33.33%
    const equipSections = sections.filter(s =>
      /^\d{1,3}\s+\d{1,2}\.\d{2}\s+\d{1,2}\.\d{2}$/.test(s)
    );

    let motor = null;
    let boat  = null;

    if (equipSections.length >= 2) {
      // 後ろから2番目 = モーター, 最後 = ボート
      const motorParts = equipSections[equipSections.length - 2].split(/\s+/);
      const boatParts  = equipSections[equipSections.length - 1].split(/\s+/);
      motor = motorParts[1] ?? null; // 2連率
      boat  = boatParts[1]  ?? null;
    } else if (equipSections.length === 1) {
      const parts = equipSections[0].split(/\s+/);
      motor = parts[1] ?? null;
    }

    racers.push({ cls: classMatch[1], motor, boat });
  }

  return racers.length >= 6 ? racers : null;
}

export default async function handler(req) {
  const headers = {
    'Content-Type'                : 'application/json',
    'Access-Control-Allow-Origin' : '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
  };

  if (req.method === 'OPTIONS') return new Response(null, { status: 200, headers });

  const { searchParams } = new URL(req.url);
  const hd  = searchParams.get('hd');
  const jcd = searchParams.get('jcd');
  const rno = searchParams.get('rno');

  if (!hd || !jcd || !rno) {
    return new Response(
      JSON.stringify({ error: 'hd・jcd・rno パラメータが必要です' }),
      { status: 400, headers }
    );
  }

  // 出走表 → 直前情報 の順に試す
  const targets = [
    {
      label    : '出走表',
      boatUrl  : `https://www.boatrace.jp/owpc/pc/race/racelist?hd=${hd}&jcd=${jcd}&rno=${rno}`,
    },
    {
      label    : '直前情報',
      boatUrl  : `https://www.boatrace.jp/owpc/pc/race/beforeinfo?hd=${hd}&jcd=${jcd}&rno=${rno}`,
    },
  ];

  let lastError = 'データが取得できませんでした';

  for (const { label, boatUrl } of targets) {
    try {
      // Jina AI リーダー経由でアクセス（IPブロック回避）
      const jinaUrl = `https://r.jina.ai/${boatUrl}`;

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 12000);

      let resp;
      try {
        resp = await fetch(jinaUrl, {
          signal : controller.signal,
          headers: {
            'Accept'         : 'text/plain,text/markdown,*/*',
            'X-Return-Format': 'markdown',
          },
        });
      } finally {
        clearTimeout(timer);
      }

      if (!resp.ok) { lastError = `${label}: HTTP ${resp.status}`; continue; }

      const text = await resp.text();

      // 級別マーカーがあるか確認
      if (!/\/\s*[AB][12]/.test(text)) {
        lastError = `${label}: 出走データが見つかりません（開催日・競艇場を確認）`;
        continue;
      }

      const racers = parseJinaMarkdown(text);
      if (!racers) { lastError = `${label}: 解析失敗`; continue; }

      return new Response(
        JSON.stringify({ ok: true, source: label, racers }),
        { headers }
      );

    } catch (err) {
      lastError = `${label}: ${err.message}`;
    }
  }

  return new Response(JSON.stringify({ error: lastError }), { status: 404, headers });
}
