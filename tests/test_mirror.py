from devinducky import oled


TITLE = "[AH-42] R4-N10 Subjectivity upload times out (504) while verification completes"


def test_title_rows_wrap_and_truncate():
    assert oled.title_rows(TITLE) == ["[AH-42] R4-N10", "Subjectivity", "upload times out", "(504) while..."]
    assert oled.title_rows("Short one") == ["Short one"]
    assert oled.title_rows("café ☕") == ["caf? ?"]
    assert all(len(r) <= oled.COLS for r in oled.title_rows("x" * 200))


def test_screen_gvs_title_then_status_little_endian():
    gvs = oled.screen_gvs("ABCD", "Waiting for you: pick one")
    assert sorted(gvs) == list(range(27))
    assert gvs[0] == 0x44434241 and gvs[1] == 0x20202020
    raw = b"".join(gvs[i].to_bytes(4, "little") for i in range(27)).decode()
    assert raw[72:90].rstrip() == "Waiting for you:" and raw[90:108].rstrip() == "pick one"


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


def test_press_sends_screen_between_cleared_and_set_handshake(monkeypatch):
    d, writes = _daemon(monkeypatch)
    d.recents = type("R", (), {"last_devin_text": {"aaa": "Which one?"}})()
    d.sessions["aaa"].update(status="running", status_detail="waiting_for_user")
    d.send_title(1)
    assert writes == [{27: 0}, oled.screen_gvs("Fix the bug", "Waiting for you: Which one?"), {27: 1}]


def test_press_on_empty_slot_says_so(monkeypatch):
    d, writes = _daemon(monkeypatch)
    d.recents = None
    d.send_title(2)
    assert writes == [{27: 0}, oled.screen_gvs("(no session)", ""), {27: 2}]


def test_open_event_opens_in_app_and_press_does_nothing_on_the_mac(monkeypatch):
    d, _ = _daemon(monkeypatch)
    opened = []
    monkeypatch.setattr(d, "open_slot", opened.append, raising=False)
    d.last_open = {}
    d.handle("press", 1)
    assert opened == []
    d.handle("open", 1)
    d.handle("open", 1)                   # pad repeating itself: ignored
    assert opened == [0]


# ---------- pad scripts ----------

def test_slot_title_script_redraws_opens_and_passes_keys_through():
    from devinducky import cli, keys
    lines = cli.slot_title_script(3).split("\n")
    assert lines[0] == "VAR want = 3" and "VAR last = 3" in lines   # its own key counts as held
    assert "IF p != last" in lines
    assert "IF _GV27 == want && shown != want" in lines
    assert "POKE32(62464, _GV0)" in lines and "POKE32(62568, _GV26)" in lines and "POKE8(62572, 0)" in lines
    assert "PUTS(62536 | (18 << 16) | (1 << 29))" in lines          # 5th row (status) starts at byte 72
    assert not any(l.startswith("_GV") for l in lines)                  # scripts never write GVs
    i = lines.index("IF p == want")
    assert lines[i + 1:i + 4] == ["IF want == 1", keys.HOTKEY_FOR[keys.Event("open", 1)].ducky(), "END_IF"]
    assert "closing = 1" in lines and "done = 1" in lines
    assert keys.HOTKEY_FOR[keys.Event("press", 7)].ducky() not in lines # another slot only closes
    assert keys.HOTKEY_FOR[keys.Event("press", keys.LAUNCH_KEYS[0])].ducky() in lines   # other keys still work
    assert lines[-5:] == ["OLED_RESTORE", "KEYUP CTRL", "KEYUP ALT", "KEYUP SHIFT", "KEYUP COMMAND"]
    assert "LBREAK" not in lines and "CONTINUE" not in lines                 # these misbehaved on the pad
    assert sum(l.startswith("WHILE") for l in lines) == 1                   # one flat loop
    declared = [l.split()[1] for l in lines if l.startswith("VAR ")]
    assert len(declared) == len(set(declared))                          # the compiler rejects re-declaring


def test_recents_keeps_devins_last_message():
    from devinducky.recents import Recents

    class Dv(FakeDevin):
        def session_messages(self, sid):
            return [{"created_at": 5, "source": "devin", "message": "first"},
                    {"created_at": 6, "source": "user", "message": "hi"},
                    {"created_at": 7, "source": "devin", "message": "Which items?"}]

    r = Recents(Dv([_s("a", 1, 7)], {}))
    r.refresh()
    assert r.last_devin_text == {"a": "Which items?"}


def test_open_while_another_is_opening_is_dropped(monkeypatch):
    from devinducky import daemon, desktop
    d, _ = _daemon(monkeypatch)
    d.nav_lock = __import__("threading").Lock()
    calls = []
    monkeypatch.setattr(desktop, "open_session", lambda sid: calls.append(sid) or "sidebar")
    d.nav_lock.acquire()
    d._open("S1", "aaa")
    assert calls == []
    d.nav_lock.release()
    d._open("S1", "aaa")
    assert calls == ["aaa"]
