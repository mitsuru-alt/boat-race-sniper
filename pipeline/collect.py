"""公式データ（番組表 B / 競走成績 K）を取得して、1日1ファイルのCSVに保存する。

使い方:
    python pipeline/collect.py                 # 昨日と一昨日（日本時間）を取得
    python pipeline/collect.py --start 2024-10-01 --end 2026-10-02   # 期間を一括取得

出力: data/entries/YYYY/YYYYMMDD.csv.gz（1行 = 1レースの1艇）
同じ日を何度取り直しても上書きされるだけなので安全。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "entries"
JST = timezone(timedelta(hours=9))

FW_TO_HW = str.maketrans("０１２３４５６７８９．", "0123456789.")

VENUE_CODES = {
    "桐生": "01", "戸田": "02", "江戸川": "03", "平和島": "04", "多摩川": "05",
    "浜名湖": "06", "蒲郡": "07", "常滑": "08", "津": "09", "三国": "10",
    "びわこ": "11", "住之江": "12", "尼崎": "13", "鳴門": "14", "丸亀": "15",
    "児島": "16", "宮島": "17", "徳山": "18", "下関": "19", "若松": "20",
    "芦屋": "21", "福岡": "22", "唐津": "23", "大村": "24",
}
# 長い名前から照合（"津" が他の名前に含まれる誤検出を防ぐ）
_VENUES_BY_LEN = sorted(VENUE_CODES, key=len, reverse=True)

COLUMNS = [
    "date", "venue", "race_no", "race_name", "lane", "racer_no", "cls", "age", "weight",
    "nat_win", "nat_2", "loc_win", "loc_2", "motor_no", "motor_2", "boat_no", "boat_2",
    "ex_time", "course", "st", "finish",
    "weather", "wind_dir", "wind_speed", "wave",
    "tri_combo", "tri_payout", "tri_pop",
]

log = logging.getLogger("collect")
IN_ACTIONS = bool(__import__("os").environ.get("GITHUB_ACTIONS"))


def annotate(level: str, msg: str) -> None:
    """GitHub Actions の画面（とAPI）から見えるメッセージを出す。"""
    if IN_ACTIONS:
        enc = msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::{level}::{enc}", flush=True)


# ----------------------------------------------------------------------
# 番組表（B ファイル）の解析
# ----------------------------------------------------------------------

# 例: "1 4444桐生順平36埼玉52A1 7.85 59.42 8.10 63.64 34 38.26 50 32.73 1 3 2"
_B_ENTRY = re.compile(
    r"^\s*([1-6])\s*(\d{4})"          # 艇番, 登録番号
    r"(.+?)"                          # 選手名（全角スペース含む）
    r"(\d{2})(\D{1,3}?)(\d{2})"       # 年齢, 支部, 体重
    r"([AB][12])\s*"                  # 級別
    r"(\d{1,2}\.\d{2})\s+(\d{1,3}\.\d{2})\s+"   # 全国勝率, 全国2連率
    r"(\d{1,2}\.\d{2})\s+(\d{1,3}\.\d{2})\s+"   # 当地勝率, 当地2連率
    r"(\d{1,3})\s+(\d{1,3}\.\d{2})\s+"           # モーターNo, モーター2連率
    r"(\d{1,3})\s+(\d{1,3}\.\d{2})"              # ボートNo, ボート2連率
)
_B_RACE_HEAD = re.compile(r"^\s*([０-９\d]{1,2})\s*[ＲR](?:\s|　)")
_B_DATE = re.compile(r"([０-９\d]{4})\s*年\s*([０-９\d]{1,2})\s*月\s*([０-９\d]{1,2})\s*日")


def _venue_in(line: str) -> str | None:
    normalized = re.sub(r"[\s　]+", "", line)
    if "ボートレース" not in normalized:
        return None
    for name in _VENUES_BY_LEN:
        if f"ボートレース{name}" in normalized:
            return VENUE_CODES[name]
    return None


def parse_b_entry(line: str) -> dict | None:
    """番組表の選手行を1行解析する。合わなければ None。"""
    m = _B_ENTRY.match(line)
    if not m:
        return None
    g = m.groups()
    return {
        "lane": int(g[0]),
        "racer_no": int(g[1]),
        "age": int(g[3]),
        "weight": int(g[5]),
        "cls": g[6],
        "nat_win": float(g[7]),
        "nat_2": float(g[8]),
        "loc_win": float(g[9]),
        "loc_2": float(g[10]),
        "motor_no": int(g[11]),
        "motor_2": float(g[12]),
        "boat_no": int(g[13]),
        "boat_2": float(g[14]),
    }


_SECTION_MARK = re.compile(r"^\s*(\d{2})([BK])(BGN|END)\s*$")


def parse_b_text(text: str) -> dict[tuple[str, int, int], dict]:
    """番組表テキスト全体 → {(場コード, R, 艇番): 選手データ}

    場の区切りは公式ファイルの目印「24BBGN」〜「24BEND」（数字=場コード）で判定する。
    場名の表記ゆれ（ひらがな表記・全角スペース）や「唐津」と「津」の取り違えを避けるため。
    目印が無い古い形式だけ、見出しの場名で判定する。
    """
    out: dict[tuple[str, int, int], dict] = {}
    venue: str | None = None
    race_no: int | None = None
    has_marks = any(_SECTION_MARK.match(l) for l in text.splitlines())
    for raw in text.splitlines():
        line = raw.rstrip()
        mk = _SECTION_MARK.match(line)
        if mk:
            venue = mk.group(1) if mk.group(3) == "BGN" and mk.group(1) in VENUE_CODES.values() else None
            race_no = None
            continue
        if not has_marks:
            v = _venue_in(line)
            if v:
                venue, race_no = v, None
                continue
        if venue is None:
            continue
        h = _B_RACE_HEAD.match(line)
        if h:
            race_no = int(h.group(1).translate(FW_TO_HW))
            continue
        if race_no is None:
            continue
        e = parse_b_entry(line)
        if e:
            out[(venue, race_no, e["lane"])] = e
    return out


# ----------------------------------------------------------------------
# 競走成績（K ファイル）
# 1レース分の解析は boatrace-lzh の PerformanceParser に任せ、
# 場の区切りは目印「24KBGN」〜「24KEND」で自分で行う（ライブラリは場名で判定するため
# 「唐津」が「津」として記録されてしまう）。
# ----------------------------------------------------------------------

def split_k_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    venue: str | None = None
    for raw in text.splitlines():
        mk = _SECTION_MARK.match(raw)
        if mk and mk.group(2) == "K":
            venue = mk.group(1) if mk.group(3) == "BGN" and mk.group(1) in VENUE_CODES.values() else None
            if venue:
                sections.setdefault(venue, [])
            continue
        if venue:
            sections[venue].append(raw.strip())
    return sections


def parse_k_files(files: dict[str, str]):
    from boatrace_lzh import PerformanceParser

    parser = PerformanceParser()
    races, entries, trifecta = {}, {}, {}
    for text in files.values():
        sections = split_k_sections(text)
        if not sections:
            continue
        for venue, lines in sections.items():
            race_date = parser._extract_date(lines)
            for race_no, race_lines in parser._split_into_races(lines).items():
                parsed = parser._parse_race(race_lines, venue, race_date, race_no)
                if not parsed:
                    continue
                race, _racers, ents, pays = parsed
                races[(venue, race_no)] = race
                for e in ents:
                    entries[(venue, race_no, e.boat_number)] = e
                for p in pays:
                    if p.ticket_type == "sanrensho":
                        trifecta[(venue, race_no)] = p
    return races, entries, trifecta


def build_rows(day: date, b_map: dict, k_races: dict, k_entries: dict, k_tri: dict) -> list[dict]:
    rows = []
    keys = set(b_map) | set(k_entries)
    for venue, race_no, lane in sorted(keys):
        b = b_map.get((venue, race_no, lane), {})
        k = k_entries.get((venue, race_no, lane))
        r = k_races.get((venue, race_no))
        t = k_tri.get((venue, race_no))
        if not b and k is None:
            continue
        row = {c: "" for c in COLUMNS}
        row.update({"date": day.isoformat(), "venue": venue, "race_no": race_no, "lane": lane})
        row.update(b)
        if k is not None:
            if not row["racer_no"]:
                row["racer_no"] = k.racer_number
            row["ex_time"] = _s(k.exhibition_time)
            row["course"] = _s(k.entrance_position)
            row["st"] = _s(k.st_timing)
            row["finish"] = _s(k.result_position)
        if r is not None:
            row["race_name"] = r.race_name or ""
            row["weather"] = r.weather or ""
            row["wind_dir"] = _s(r.wind_direction)
            row["wind_speed"] = _s(r.wind_speed)
            row["wave"] = _s(r.wave_height)
        if t is not None:
            row["tri_combo"] = t.winning_combination
            row["tri_payout"] = t.payout
            row["tri_pop"] = _s(t.popularity)
        rows.append(row)
    return rows


def _s(v) -> str:
    return "" if v is None else str(v)


def write_day(day: date, rows: list[dict]) -> Path:
    path = DATA_DIR / f"{day.year}" / f"{day:%Y%m%d}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return path


def collect_day(dl, day: date) -> dict:
    b_files = dl.download(day, "schedule")
    k_files = dl.download(day, "performance")
    b_map: dict = {}
    for text in b_files.values():
        b_map.update(parse_b_text(text))
    k_races, k_entries, k_tri = parse_k_files(k_files) if k_files else ({}, {}, {})

    stats = {
        "date": day.isoformat(),
        "b_entries": len(b_map),
        "k_entries": len(k_entries),
        "k_races": len(k_races),
        "trifecta": len(k_tri),
    }
    if b_files and not b_map:
        # 解析できなかった時は原因調査用に冒頭を出す
        sample = next(iter(b_files.values()))
        head = "\n".join(sample.splitlines()[:40])
        log.warning("番組表の解析が0件: %s\n---\n%s\n---", day, head)
        annotate("warning", f"番組表の解析0件 {day}\n{head}")
    if k_files and not k_entries:
        sample = next(iter(k_files.values()))
        head = "\n".join(sample.splitlines()[:60])
        log.warning("成績の解析が0件: %s\n---\n%s\n---", day, head)
        annotate("warning", f"成績の解析0件 {day}\n{head}")
    if not b_files and not k_files:
        annotate("warning", f"{day} ファイルを取得できず（開催なし or 未公開 or 接続不可）")

    if not b_map and not k_entries:
        stats["written"] = 0
        return stats
    rows = build_rows(day, b_map, k_races, k_entries, k_tri)
    write_day(day, rows)
    stats["written"] = len(rows)
    if __import__("os").environ.get("DEBUG_SAMPLE") and not getattr(collect_day, "_sampled", False):
        collect_day._sampled = True
        for kind, files in (("番組表", b_files), ("成績", k_files)):
            if files:
                text = next(iter(files.values()))
                annotate("notice", f"{kind}サンプル {day}\n" + "\n".join(text.splitlines()[:45]))
        complete = [r for r in rows if r["nat_win"] != "" and r["finish"] != ""][:6]
        annotate("notice", "解析結果サンプル\n" + "\n".join(str(r) for r in complete))
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="開始日 YYYY-MM-DD")
    ap.add_argument("--end", help="終了日 YYYY-MM-DD（含む）")
    ap.add_argument("--delay", type=float, default=1.0, help="ダウンロード間隔（秒）")
    ap.add_argument("--skip-existing", action="store_true", help="既に保存済みの日は飛ばす")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    today = datetime.now(JST).date()
    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else today - timedelta(days=1)
    else:
        start, end = today - timedelta(days=2), today - timedelta(days=1)

    from boatrace_lzh import LzhDownloader

    dl = LzhDownloader(cache_dir=ROOT / ".cache" / "lzh", request_delay=args.delay, max_workers=1)

    total = 0
    zero_parse_days = 0
    summary: list[str] = []
    day = start
    while day <= end:
        path = DATA_DIR / f"{day.year}" / f"{day:%Y%m%d}.csv.gz"
        if args.skip_existing and path.exists():
            day += timedelta(days=1)
            continue
        try:
            st = collect_day(dl, day)
        except Exception as e:  # 1日の失敗で全体を止めない
            log.error("%s 失敗: %s", day, e)
            annotate("error", f"{day} 失敗: {type(e).__name__}: {e}")
            day += timedelta(days=1)
            continue
        log.info("%(date)s 番組%(b_entries)4d 成績%(k_entries)4d 3連単%(trifecta)3d → %(written)d行", st)
        summary.append("{date} 番組{b_entries} 成績{k_entries} 3連単{trifecta} → {written}行".format(**st))
        total += st["written"]
        if st["written"] and (st["b_entries"] == 0 or st["k_entries"] == 0):
            zero_parse_days += 1
        day += timedelta(days=1)
        time.sleep(args.delay)

    log.info("合計 %d 行", total)
    annotate("notice", f"収集 {start}〜{end}: 合計 {total:,} 行（片方しか読めなかった日 {zero_parse_days}）\n" + "\n".join(summary[-10:]))
    if total == 0:
        log.error("1行も取得できませんでした。ログを確認してください。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
