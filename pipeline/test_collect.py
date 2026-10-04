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


# 実ファイルと同じ「場コード+BBGN」目印つき。場名がひらがな・唐津/津が並ぶケース
SAMPLE_B_MARKED = """\
STARTB
22BBGN
ボートレースふくおか   １０月　１日  福岡ルーキーシリーズ
　１Ｒ  予選　　　　          Ｈ１８００ｍ  電話投票締切予定１５：００
1 4440萩原知哉38東京55B1 4.30 23.23 4.13 12.50 51 28.57 75 31.25 665          8
22BEND
23BBGN
ボートレース唐　津   １０月　１日  からつ杯
　１Ｒ  一般　　　　          Ｈ１８００ｍ  電話投票締切予定１５：００
1 4444桐生順平36埼玉52A1 7.85 59.42 8.10 63.64 34 38.26 50 32.73
23BEND
09BBGN
ボートレース津   １０月　１日  津杯
　２Ｒ  一般　　　　          Ｈ１８００ｍ  電話投票締切予定１５：００
3 4099吉永則雄50大阪55B1 4.80 30.20 0.00  0.00 45 28.00 61 29.90 6 5
09BEND
FINALB
"""

K_SECTION = """\
   第 3日          2024/10/ 1                             ボートレース{name}

   [払戻金]       ３連単           ３連複           ２連単         ２連複
           1R  2-1-3    2260    1-2-3     580    2-1     560    1-2     220

   1R       予選　　　　                 H1800m  晴　  風  北西　 1m  波　  1cm
  着 艇 登番 　選　手　名　　ﾓｰﾀｰ ﾎﾞｰﾄ 展示 進入 ｽﾀｰﾄﾀｲﾐﾝｸ ﾚｰｽﾀｲﾑ 差し　　　
-------------------------------------------------------------------------------
  01  2 4272 大　場　　広　孝 63   71  6.95   2    0.20     1.50.6
  02  1 4440 萩　原　　知　哉 51   75  6.90   1    0.15     1.51.4
  03  3 5264 登　　　みひ果 55   55  6.98   3    0.18     1.52.0
  04  4 4005 瀬　川　　公　則 68   65  6.93   4    0.21     1.53.1
  05  5 5003 来　田　　衣　織 43   69  6.97   5    0.22     1.54.0
  06  6 3842 星　野　　太　郎 58   73  6.99   6    0.25     1.55.0

        単勝     2          560
        ３連単   2-1-3     2260  人気     5
"""


def test_b_section_marks():
    m = parse_b_text(SAMPLE_B_MARKED)
    assert ("22", 1, 1) in m and m[("22", 1, 1)]["racer_no"] == 4440   # ひらがな表記の福岡
    assert ("23", 1, 1) in m and m[("23", 1, 1)]["racer_no"] == 4444   # 唐津が津にならない
    assert ("09", 2, 3) in m
    assert len(m) == 3


def test_k_sections_karatsu_not_tsu():
    from collect import parse_k_files
    text = ("STARTK\n23KBGN\n唐　津［成績］\n" + K_SECTION.format(name="唐　津") + "23KEND\n"
            "09KBGN\n津［成績］\n" + K_SECTION.format(name="津") + "09KEND\nFINALK\n")
    races, entries, tri = parse_k_files({"k.txt": text})
    assert ("23", 1) in races and ("09", 1) in races
    assert len(entries) == 12
    assert entries[("23", 1, 2)].result_position == 1 and entries[("23", 1, 2)].st_timing == 0.20
    assert tri[("23", 1)].winning_combination == "2-1-3" and tri[("23", 1)].payout == 2260
    assert races[("23", 1)].wind_speed == 1.0



def test_three_digit_boat_no_glued_to_motor_rate():
    # 福岡・芦屋の実データ（ボート番号が3桁で、モーター2連率とくっついている）
    e = parse_b_entry("1 3611岩崎芳美54徳島49A2 6.00 39.29 5.50 38.89 36 31.90154 36.11 155         12")
    assert e["motor_no"] == 36 and e["motor_2"] == 31.90 and e["boat_no"] == 154 and e["boat_2"] == 36.11
    e = parse_b_entry("5 4897深見亜由34愛知44B1 3.86 14.77 0.00  0.00  4 41.90157 35.58 663          5")
    assert e["motor_no"] == 4 and e["boat_no"] == 157 and e["loc_win"] == 0.0
    e = parse_b_entry("1 3303渡辺　豊59東京54B1 4.33 18.97 6.18 36.36 19 32.79124 31.18 442 224     10")
    assert e["racer_no"] == 3303 and e["boat_no"] == 124 and e["boat_2"] == 31.18


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK", name)
