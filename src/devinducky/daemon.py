"""The devinducky daemon: owns the pad, mirrors your recent Devin sessions, reacts to keys."""

import json
import logging
import queue
import subprocess
import threading
import time
import urllib.request

from . import auth, desktop, hid, keys, oled, state
from .devin import Devin, DevinError
from .input import PadInput
from .recents import Recents
from .render import Frame, Renderer, WHITE

log = logging.getLogger("devinducky")

GREEN, RED, DIM = (0, 255, 60), (255, 0, 0), (40, 40, 40)
CURSOR_TIMEOUT_S = 10
GOTO_SETTLE_S = 0.35            # open the cursor's session once the wheel stops turning
API_OK, API_AUTH, API_NET = 0, 1, 2
GV_HANDSHAKE = oled.GV_HANDSHAKE


def notify(title, message):
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True)


def title_of(session):
    return session.get("title") or f"(untitled) {session['session_id'][:6]}"


class Daemon:
    def __init__(self, cfg, dry_pad=False):
        self.cfg = cfg
        self.dry_pad = dry_pad
        self.lock = threading.RLock()
        self.slots = [None] * keys.NUM_SLOTS   # slot index -> session_id, mirrors the app's sidebar
        self.sessions = {}            # session_id -> latest session dict
        self.states = {}              # session_id -> display state
        self.finished_since = {}      # session_id -> unix ts first seen finished
        self.cursor = None            # slot index
        self.cursor_moved = 0.0
        self.goto_due = None          # monotonic time to open the cursor's session
        self.nav_lock = threading.Lock()   # one app switch at a time, in order
        self.armed = {}               # launch index -> monotonic deadline
        self.api_health = API_OK
        self.failures = 0
        self.fast_until = 0.0
        self.poll_now = threading.Event()
        self.frame = Frame()
        self.renderer = Renderer(cfg["resync_s"], cfg["pad_profile"])
        self.devin = None
        self.recents = None
        self.input = PadInput(cfg["hold_ms"], on_raw=self._on_raw)

    # ---------- Devin polling ----------

    def connect(self):
        while True:
            try:
                self.devin = Devin(auth.api_key())
                me = self.devin.whoami()
                log.info("authenticated as %s (%s) in %s", me.get("user_name") or me.get("service_user_name"),
                         me.get("principal_type"), self.devin.org_id)
                self.recents = Recents(self.devin, keys.NUM_SLOTS, self.cfg["hide_origins"], self.cfg["list_first"])
                self.api_health = API_OK
                return
            except auth.NoApiKey as e:
                log.error("%s", e)
                self.api_health = API_AUTH
            except DevinError as e:
                log.error("Devin auth check failed: %s", e)
                self.api_health = API_AUTH if e.status in (401, 403) else API_NET
            time.sleep(30)

    def poll_once(self):
        top = self.recents.refresh()
        with self.lock:
            prev = dict(self.states)
            old_slots = list(self.slots)
            for s in top:
                self._update_session(s)
            self.slots = [s["session_id"] for s in top] + [None] * (keys.NUM_SLOTS - len(top))
            on_keys = {sid: self.states[sid] for sid in self.slots if sid}
            changes = state.transitions(prev, on_keys)
        if self.slots != old_slots:
            log.info("slots: %s", " | ".join(f"S{i + 1} {title_of(self.sessions[sid])[:30]}"
                                             for i, sid in enumerate(self.slots) if sid))
        for sid, _old, new in changes:
            self._notify_transition(sid, new)

    def _update_session(self, s):
        sid = s["session_id"]
        st = state.display_state(s)
        self.sessions[sid] = s
        if st == "finished" and self.states.get(sid) != "finished":
            # First sighting: if we only just started, trust updated_at instead of "now".
            seen_before = sid in self.states
            self.finished_since[sid] = state.now_ts() if seen_before else (
                state.parse_ts(s.get("updated_at")) or state.now_ts())
        self.states[sid] = st

    def slot_of(self, sid):
        return self.slots.index(sid) if sid in self.slots else None

    def _notify_transition(self, sid, new):
        kind = {"waiting": "waiting", "approval": "waiting", "finished": "finished", "error": "error"}[new]
        if not self.cfg["notify"].get(kind):
            return
        s = self.sessions.get(sid, {"session_id": sid})
        slot = self.slot_of(sid)
        label = f"S{slot + 1} · " if slot is not None else ""
        title = title_of(s)
        if new == "finished":
            prs = len(s.get("pull_requests") or [])
            msg = f"{label}finished: {title}" + (f" ({prs} PR{'s' if prs != 1 else ''})" if prs else "")
        elif new == "error":
            msg = f"{label}error: {title} ({s.get('status_detail') or s.get('status')})"
        else:
            msg = f"{label}Devin needs you: {title}" + (" (approval)" if new == "approval" else "")
        log.info("notify: %s", msg)
        notify("Devin", msg)
        if self.cfg["ntfy_url"]:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    self.cfg["ntfy_url"], data=msg.encode(), method="POST"), timeout=5)
            except OSError as e:
                log.warning("ntfy failed: %s", e)

    def poll_loop(self):
        self.connect()
        backoff = 0
        first = True
        while True:
            if not first:
                fast = time.monotonic() < self.fast_until
                self.poll_now.wait(backoff or (self.cfg["fast_poll_s"] if fast else self.cfg["poll_s"]))
                self.poll_now.clear()
            first = False
            try:
                self.poll_once()
                self.failures, backoff, self.api_health = 0, 0, API_OK
            except DevinError as e:
                self.failures += 1
                if e.status == 429:
                    backoff = min(60, max(5, backoff * 2))
                    log.warning("rate limited; backing off %ss", backoff)
                elif e.status in (401, 403):
                    self.api_health = API_AUTH
                    log.error("auth error: %s", e)
                else:
                    log.warning("poll failed (%d): %s", self.failures, e)
                    if self.failures >= 3:
                        self.api_health = API_NET

    # ---------- key handling ----------

    def _on_raw(self, ev, _now):
        self.renderer.invalidate(keys.led_of_key(ev.key))
        if ev.kind == "press" and keys.slot_of(ev.key) is not None:
            threading.Thread(target=self.send_title, args=(ev.key,), daemon=True).start()

    def send_title(self, key):
        """Put slot `key`'s title in _GV0.._GV19 for the pad's title screen, then say whose it is.

        The title screen (cli.slot_title_script) keeps running while it's up and
        redraws once _GV28 names the key it's waiting for; _GV28 is cleared first so
        it never draws an older title for that key.
        """
        with self.lock:
            sid = self.slots[keys.slot_of(key)]
            s = self.sessions.get(sid) if sid else None
            said = self.recents.last_devin_text.get(sid) if (s and self.recents) else None
        title, status = (title_of(s), state.status_text(s, said)) if s else ("(no session)", "")
        if self.dry_pad:
            log.debug("screen for key %d: %r / %r", key, title, status)
            return

        def write(pad):
            pad.write_gv({GV_HANDSHAKE: 0})
            pad.write_gv(oled.screen_gvs(title, status))
            pad.write_gv({GV_HANDSHAKE: key})

        self._urgent_write(write, f"title for key {key}")

    def _urgent_write(self, fn, what):
        hid.PRIORITY.set()
        try:
            hid.with_retry(fn, attempts=2, delay_s=0.02)
        except (hid.PadBusy, hid.PadNotFound, hid.PadError, OSError) as e:
            log.info("%s not sent: %s", what, e)
        finally:
            hid.PRIORITY.clear()

    def handle(self, kind, key):
        log.debug("event %s %s", kind, key)
        slot = keys.slot_of(key)
        if slot is not None:
            if kind == "open":           # tapped again while its title was showing
                if self.slots[slot]:
                    self.frame.set_effect(keys.led_of_slot(slot), "fade", WHITE, 0.6)
                self.open_slot(slot)
            # a first tap only shows the title, which the pad draws itself
            return
        launch = keys.launch_of(key)
        if launch is not None:
            self.tap_launch(launch)
        elif key in (keys.KEY_OPEN, keys.KEY_DASH):
            self.frame.set_effect(keys.led_of_key(key), "fade", WHITE, 0.6)
            threading.Thread(target=self._activate_app, daemon=True).start()
        elif key == keys.ENC1_PUSH:
            target = self.cursor if self.cursor is not None else self.most_urgent_slot()
            if target is not None:
                self.open_slot(target)
        elif key in (keys.KEY_REFRESH, keys.ENC2_PUSH):
            self.poll_now.set()
            self.frame.set_effect(keys.led_of_key(keys.KEY_REFRESH), "fade", WHITE, 0.6)
        elif key in (keys.ENC1_CW, keys.ENC1_CCW):
            self.move_cursor(1 if key == keys.ENC1_CW else -1)

    def _activate_app(self):
        try:
            desktop.activate()
        except (desktop.DesktopError, subprocess.TimeoutExpired) as e:
            log.warning("couldn't bring the Devin app forward: %s", e)

    def open_slot(self, slot):
        sid = self.slots[slot]
        if not sid:
            self.frame.set_effect(keys.led_of_slot(slot), "fade", DIM, 0.3)
            return
        threading.Thread(target=self._open, args=(f"S{slot + 1}", sid), daemon=True).start()

    def _open(self, label, sid):
        with self.nav_lock:
            t = time.monotonic()
            how = desktop.open_session(sid)
            if how:
                log.info("%s: shown in the Devin app via %s (%.2fs)", label, how, time.monotonic() - t)
            else:
                log.warning("%s: couldn't show %s in the Devin app", label, sid)

    def occupied(self):
        return [i for i, sid in enumerate(self.slots) if sid]

    def most_urgent_slot(self):
        with self.lock:
            ranked = sorted(self.occupied(),
                            key=lambda sl: state.URGENCY.index(self.states.get(self.slots[sl], "ended")))
        return ranked[0] if ranked else None

    def move_cursor(self, step):
        used = self.occupied()
        if not used:
            return
        if self.cursor not in used:
            self.cursor = used[0] if step > 0 else used[-1]
        else:
            self.cursor = used[(used.index(self.cursor) + step) % len(used)]
        self.cursor_moved = time.monotonic()
        self.goto_due = self.cursor_moved + GOTO_SETTLE_S

    def tap_launch(self, idx):
        led = keys.led_of_key(keys.LAUNCH_KEYS[idx])
        presets = self.cfg["launch"]
        if idx >= len(presets):
            self.frame.set_effect(led, "fade", DIM, 0.3)
            return
        now = time.monotonic()
        if self.armed.get(idx, 0) > now:
            del self.armed[idx]
            self.frame.set_effect(led, "solid", WHITE, 30)
            threading.Thread(target=self.launch, args=(idx,), daemon=True).start()
        else:
            self.armed[idx] = now + self.cfg["arm_s"]
            self.frame.set_effect(led, "blink", WHITE, self.cfg["arm_s"], period=0.2)
            log.info("L%d armed: %s (tap again to launch)", idx + 1, presets[idx].get("name"))

    def launch(self, idx):
        preset = self.cfg["launch"][idx]
        led = keys.led_of_key(keys.LAUNCH_KEYS[idx])
        try:
            s = self.devin.create_session(preset)
        except DevinError as e:
            log.error("launch L%d failed: %s", idx + 1, e)
            notify("devinducky", f"Launch '{preset.get('name')}' failed: {e}")
            self.frame.set_effect(led, "flash", RED, 1.5)
            return
        sid = s.get("session_id")
        log.info("L%d launched %s", idx + 1, sid)
        self.frame.set_effect(led, "flash", GREEN, 1.5)
        with self.lock:
            self.fast_until = time.monotonic() + self.cfg["fast_poll_window_s"]
        if sid:
            self._open(f"L{idx + 1}", sid)
        self.poll_now.set()

    # ---------- rendering ----------

    def compose(self, t):
        cfg_c = self.cfg["colors"]
        f = self.frame
        f.pulses = {}
        now_wall = state.now_ts()
        slot_states = []
        with self.lock:
            for slot in range(keys.NUM_SLOTS):
                led = keys.led_of_slot(slot)
                sid = self.slots[slot]
                st = self.states.get(sid, "starting") if sid else "unbound"
                if sid:
                    slot_states.append(st)
                color = cfg_c[st]
                if st == "finished" and now_wall - self.finished_since.get(sid, now_wall) > self.cfg["finished_dim_s"]:
                    color = cfg_c["finished_dim"]
                f.base[led] = tuple(color)
                if st in state.PULSE:
                    f.pulses[led] = state.PULSE[st]
            for i, k in enumerate(keys.LAUNCH_KEYS):
                preset = self.cfg["launch"][i] if i < len(self.cfg["launch"]) else None
                f.base[keys.led_of_key(k)] = tuple(preset.get("color", cfg_c["launch"])) if preset else (0, 0, 0)
            for k in (keys.KEY_OPEN, keys.KEY_DASH, keys.KEY_REFRESH, keys.KEY_INFO):
                f.base[keys.led_of_key(k)] = tuple(cfg_c["nav"])
            info = keys.led_of_key(keys.KEY_INFO)
            if self.api_health != API_OK:
                f.base[info] = RED
                f.pulses[info] = 0.8
            if self.cursor is not None and (t - self.cursor_moved > CURSOR_TIMEOUT_S
                                            or self.slots[self.cursor] is None):
                self.cursor = None
            f.cursor = keys.led_of_slot(self.cursor) if self.cursor is not None else None
            gvs = state.summary_gvs(slot_states, self.api_health)
        return f.evaluate(t), gvs

    def render_loop(self):
        # Each LED write is a ~30 ms USB round trip, so a full repaint takes ~0.6 s;
        # it runs here so key handling never waits on the pad.
        last_dry = None
        while True:
            t = time.monotonic()
            leds, gvs = self.compose(t)
            if self.dry_pad:
                snap = (tuple(leds), tuple(sorted(gvs.items())))
                if snap != last_dry:
                    log.debug("frame %s gvs %s", leds[:12], gvs)
                    last_dry = snap
            else:
                self.renderer.push(leds, gvs, t)
            time.sleep(0.08 if self.frame.animating(t) else 0.25)

    def run(self):
        logging.getLogger("devinducky").info("starting; mirroring your recent sessions")
        self.input.start()
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.render_loop, daemon=True).start()
        while True:
            try:
                kind, key = self.input.events.get(timeout=0.1)
                self.handle(kind, key)
            except queue.Empty:
                pass
            t = time.monotonic()
            if self.goto_due and t >= self.goto_due:
                self.goto_due = None
                if self.cursor is not None:
                    self.open_slot(self.cursor)
            for idx, deadline in list(self.armed.items()):
                if deadline <= t:
                    del self.armed[idx]
