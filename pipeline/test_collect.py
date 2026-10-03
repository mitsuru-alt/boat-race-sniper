"""番組表パーサーのテスト（python -m pytest pipeline/ でも、直接実行でもOK）"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from collect import parse_b_entry, parse_b_text  # noqa: E402

SAMPLE_B = """\
STARTB
ボートレース住之江　　　１０月　２日　　２０２６東京・大阪・福岡三都市対抗戦　第　２日
　　　　　　　　　　　　　　　　　　　　２０２６年１０月　２日
　１２Ｒ　予選特別Ａ戦　　　　　　　　　Ｈ１８００ｍ　　電話投票締切予定２０：３１
-------------------------------------------------------------------------------
艇 選手 選手  年 支 体級    全国      当地     モーター   ボート   今節成績  早
番 登番  名   齢 部 重別 勝率  2率  勝率  2率  NO  2率  NO  2率  １２３４５６ 見
-------------------------------------------------------------------------------
1 5028原田才一32福岡52A1 6.95 52.10 7.10 55.00 34 38.26 50 32.73 1 3
2 4366前沢丈史40東京53A2 6.10 44.00 5.80 40.00 12 31.50 22 35.10 4 2
3 4099吉永則雄50大阪55B1 4.80 30.20 0.00  0.00 45 28.00 61 29.90 6 5
4 5289佃　來紀22大阪51B1 5.20 35.00 5.00 33.33 21 42.10 18 30.00 2 1
5 4351里岡右貴42大阪54A1 7.20 55.50 7.40 58.00  7 33.00  9 31.00 3 4
6 4944田代達也33福岡52B2 3.90 22.00 4.00 20.00 66 25.00 70 27.50 5 6
ボートレース津　　　　　１０月　２日　　津ＧＩ
　　　　　　　　　　　　　　　　　　　　２０２６年１０月　２日
　１Ｒ　一般戦　　　　　　　　　Ｈ１８００ｍ　　電話投票締切予定１５：００
1 4444桐生順平36埼玉52A1 7.85 59.42 8.10 63.64 34 38.26 50 32.73
FINALB
"""


def test_entry_line():
    e = parse_b_entry("1 4444桐生順平36埼玉52A1 7.85 59.42 8.10 63.64 34 38.26 50 32.73 1 3 2")
    assert e["lane"] == 1 and e["racer_no"] == 4444 and e["cls"] == "A1"
    assert e["nat_win"] == 7.85 and e["loc_2"] == 63.64
    assert e["motor_no"] == 34 and e["motor_2"] == 38.26
    assert e["boat_no"] == 50 and e["boat_2"] == 32.73
    assert e["age"] == 36 and e["weight"] == 52


def test_name_with_fullwidth_space():
    e = parse_b_entry("4 5289佃　來紀22大阪51B1 5.20 35.00 5.00 33.33 21 42.10 18 30.00")
    assert e["racer_no"] == 5289 and e["cls"] == "B1" and e["motor_2"] == 42.10


def test_single_digit_motor_no_with_padding():
    e = parse_b_entry("5 4351里岡右貴42大阪54A1 7.20 55.50 7.40 58.00  7 33.00  9 31.00")
    assert e["motor_no"] == 7 and e["boat_no"] == 9


def test_whole_file_split_by_venue_and_race():
    m = parse_b_text(SAMPLE_B)
    assert len(m) == 7
    assert ("12", 12, 1) in m and ("12", 12, 6) in m
    assert ("09", 1, 1) in m  # 津 を正しく識別
    assert m[("12", 12, 3)]["loc_win"] == 0.0


def test_header_lines_ignored():
    assert parse_b_entry("艇 選手 選手  年 支 体級    全国      当地") is None
    assert parse_b_entry("-" * 70) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
