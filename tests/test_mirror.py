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


# ---------- ranking ----------

class FakeDevin:
    def __init__(self, sessions, messages):
        self.sessions = sessions          # list of session dicts, newest-created first
        self.messages = messages          # sid -> [message created_at, ...]
        self.list_calls, self.message_calls = [], []

    def my_sessions(self, first=100, updated_after=None):
        self.list_calls.append(updated_after)
        out = [s for s in self.sessions if updated_after is None or s["updated_at"] >= updated_after]
        if updated_after is None:
            out = [s for s in out if not s.get("is_archived")]
        return out[:first]

    def session_messages(self, sid):
        self.message_calls.append(sid)
        return [{"created_at": t, "source": "devin"} for t in self.messages.get(sid, [])]


def _s(sid, created, updated, origin="desktop", archived=False):
    return {"session_id": sid, "title": sid.upper(), "created_at": created, "updated_at": updated,
            "origin": origin, "is_archived": archived}


def test_recents_rank_by_last_message_not_updated_at():
    from devinducky.recents import Recents
    dv = FakeDevin([_s("a", 30, 100), _s("b", 20, 50), _s("c", 10, 40)],
                   {"a": [31, 35], "b": [21, 49], "c": [11, 39]})
    assert [s["session_id"] for s in Recents(dv, n=2).refresh()] == ["b", "c"]


def test_recents_hide_archived_and_hidden_origins_and_rank_empty_by_created():
    from devinducky.recents import Recents
    dv = FakeDevin([_s("n", 40, 41, origin="webapp"), _s("x", 35, 36, archived=True),
                    _s("e", 30, 30), _s("m", 5, 6)],
                   {"n": [41], "x": [36], "m": [6]})
    assert [s["session_id"] for s in Recents(dv).refresh()] == ["e", "m"]   # e has no messages: created 30


def test_recents_refetch_only_changed_and_catch_old_sessions():
    from devinducky.recents import Recents
    old = _s("old", 1, 2)
    dv = FakeDevin([_s("a", 30, 31), _s("b", 20, 21)], {"a": [31], "b": [21], "old": [2]})
    r = Recents(dv, first=2)
    r.refresh()
    assert sorted(dv.message_calls) == ["a", "b"]
    dv.message_calls.clear()
    old.update(updated_at=10**10)           # someone messaged a months-old session
    dv.messages["old"] = [10**10]
    dv.sessions.append(old)
    assert [s["session_id"] for s in r.refresh()][0] == "old"
    assert dv.message_calls == ["old"]      # a and b unchanged: no refetch
    assert dv.list_calls[-1] is not None    # incremental call uses updated_after


def test_recents_drop_session_archived_later():
    from devinducky.recents import Recents
    a, b = _s("a", 30, 31), _s("b", 20, 21)
    dv = FakeDevin([a, b], {"a": [31], "b": [21]})
    r = Recents(dv)
    r.refresh()
    a.update(is_archived=True, updated_at=10**10)
    assert [s["session_id"] for s in r.refresh()] == ["b"]


# ---------- daemon ----------

def _daemon(monkeypatch):
    from devinducky import daemon, hid
    from devinducky.render import Frame
    d = daemon.Daemon.__new__(daemon.Daemon)
    import threading
    d.lock, d.dry_pad, d.frame = threading.RLock(), False, Frame()
    d.slots = ["aaa"] + [None] * 11
    d.sessions = {"aaa": {"session_id": "aaa", "title": "Fix the bug"}}
    writes = []

    class FakePad:
        def write_gv(self, values):
            writes.append(dict(values))

    monkeypatch.setattr(hid, "with_retry", lambda fn, **kw: fn(FakePad()))
    return d, writes


def test_key_down_sends_title_then_handshake(monkeypatch):
    d, writes = _daemon(monkeypatch)
    d.send_title(1)
    assert writes == [oled.title_gvs("Fix the bug"), {28: 1}]


def test_key_down_on_empty_slot_sends_only_empty_handshake(monkeypatch):
    d, writes = _daemon(monkeypatch)
    d.send_title(2)
    assert writes == [{28: 102}]


def test_hold_opens_in_app_and_tap_does_nothing_on_the_mac(monkeypatch):
    d, _ = _daemon(monkeypatch)
    opened = []
    monkeypatch.setattr(d, "open_slot", opened.append, raising=False)
    d.handle("tap", 1)
    assert opened == []
    d.handle("hold", 1)
    d.handle("hold", 2)                   # empty slot
    assert opened == [0]


# ---------- pad scripts ----------

def test_slot_release_script_sends_up_first_then_draws_title_on_tap():
    from devinducky import cli, keys
    lines = cli.slot_release_script(3, 700).splitlines()
    assert lines[0] == keys.HOTKEY_FOR[keys.Event("up", 3)].ducky()
    assert "IF _TIME_MS - _GV29 < 700 && _GV28 == 3" in lines
    assert "POKE32(62464, _GV0)" in lines and "POKE32(62540, _GV19)" in lines
    assert "POKE8(62544, 0)" in lines
    assert "PUTS(62464 | (18 << 16) | (1 << 29))" in lines
    assert "PUTS(62518 | (18 << 16) | (1 << 29))" in lines     # 4th row starts at byte 54
    assert lines[-1] == "END_IF"
