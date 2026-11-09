from devinducky import oled


TITLE = "[AH-42] R4-N10 Subjectivity upload times out (504) while verification completes"


def test_title_rows_wrap_and_truncate():
    assert oled.title_rows(TITLE) == ["[AH-42] R4-N10", "Subjectivity", "upload times out", "(504) while..."]
    assert oled.title_rows("Short one") == ["Short one"]
    assert oled.title_rows("café ☕") == ["caf? ?"]
    assert all(len(r) <= oled.COLS for r in oled.title_rows("x" * 200))


def test_title_gvs_pack_little_endian_and_pad():
    gvs = oled.title_gvs("ABCD")
    assert sorted(gvs) == list(range(20))
    assert gvs[0] == 0x44434241
    assert gvs[1] == 0x20202020          # rest of row 1 is spaces
    row2 = oled.title_gvs("x" * 18 + "y")  # second row starts at byte 18 (inside GV4)
    assert (row2[4] >> 16) & 0xFF == ord("y")
