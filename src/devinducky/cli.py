"""devinducky command line."""

import argparse
import logging
import os
import plistlib
import queue
import shutil
import subprocess
import sys
import time

from . import config, keys, oled

LABEL = "com.devinducky.daemon"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _logging(verbose, to_file=False):
    handlers = [logging.StreamHandler()]
    if to_file:
        os.makedirs(config.HOME, exist_ok=True)
        handlers = [logging.FileHandler(config.LOG_FILE)]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # one line per poll otherwise
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def cmd_run(args):
    from .daemon import Daemon
    _logging(args.verbose, to_file=args.log_file)
    Daemon(config.load(), dry_pad=args.dry_pad).run()


def cmd_test_pad(args):
    from . import hid
    from .input import PadInput
    try:
        info = hid.with_retry(lambda p: p.info())
        print(f"pad: duckyPad hw={info.hw_rev} fw={info.firmware} profile={info.profile}")
        for i in range(hid.NUM_LEDS):
            hid.with_retry(lambda p, i=i: p.set_led(i, (0, 80, 255)))
            time.sleep(0.05)
        time.sleep(0.4)
        hid.with_retry(lambda p: [p.set_led(i, (0, 0, 0)) for i in range(hid.NUM_LEDS)])
        hid.with_retry(lambda p: p.write_gv({29: 42 << 8}))
        print("LEDs: cycled blue then off; wrote 42 sessions to the INFO summary (check on the INFO key)")
    except (hid.PadNotFound, hid.PadError, hid.PadBusy) as e:
        print(f"pad HID: {e}")
    t0 = {}

    def raw(ev, now):
        if ev.kind == "down":
            t0[ev.key] = now
            print(f"  key {ev.key:>2} down")
        elif ev.kind == "up":
            ms = (now - t0.pop(ev.key, now)) * 1000
            print(f"  key {ev.key:>2} up    held {ms:.0f} ms")
        else:
            print(f"  key {ev.key:>2} press")

    pin = PadInput(config.load()["hold_ms"], on_raw=raw)
    pin.start()
    print("Press pad keys / turn encoders (Ctrl-C to quit). Nothing printing? Grant Accessibility "
          "to your terminal in System Settings › Privacy & Security.")
    try:
        while True:
            try:
                kind, key = pin.events.get(timeout=0.5)
                print(f"  -> {kind} {key}")
            except queue.Empty:
                pass
    except KeyboardInterrupt:
        pass


def cmd_recents(args):
    """Print the session each key holds (from the daemon's saved slots, updated with what's new)."""
    from . import auth, slots, state
    from .devin import Devin
    from .recents import Recents
    cfg = config.load()
    d = Devin(auth.api_key())
    d.whoami()
    top = {s["session_id"]: s for s in Recents(d, keys.NUM_SLOTS, cfg["hide_origins"], cfg["list_first"]).refresh()}
    for i, sid in enumerate(slots.assign(slots.load(config.SLOTS_FILE, keys.NUM_SLOTS), list(top))):
        s = top.get(sid)
        print(f"S{i + 1:<3} " + (f"{state.display_state(s):<9} {s.get('title') or '(untitled)'}" if s else "(empty)"))


def cmd_playbooks(args):
    from . import auth
    from .devin import Devin
    d = Devin(auth.api_key())
    d.whoami()
    for pb in d.list_playbooks() or []:
        print(f"{pb.get('playbook_id') or pb.get('id')}\t{pb.get('title') or pb.get('name')}")


def profile_scripts(cfg):
    """{(key_id, "press"|"release"): duckyScript} for the devinducky pad profile."""
    out = {}
    names = {keys.KEY_OPEN: "NEW", keys.KEY_DASH: "DASH", keys.KEY_REFRESH: "REFR"}
    for ev, hk in keys.HOTKEY_FOR.items():
        if ev.kind == "press":
            out[(ev.key, "press")] = hk.ducky()
    for k in keys.SLOT_KEYS:
        # Give the daemon time to store the title before the title screen starts:
        # a script sees the GVs as they were when it started.
        out[(k, "press")] += f"\nDELAY {TITLE_WAIT_MS}"
        out[(k, "release")] = slot_title_script(k)
    out[(keys.KEY_INFO, "press")] = INFO_SCRIPT
    out[(keys.ENC2_CW, "press")] = "MK_VOLUP"      # lower wheel is a plain volume knob,
    out[(keys.ENC2_CCW, "press")] = "MK_VOLDOWN"   # handled on the pad without the daemon
    labels = {k: f"S{k}" for k in keys.SLOT_KEYS}
    for i, k in enumerate(keys.LAUNCH_KEYS):
        labels[k] = (cfg["launch"][i].get("name", f"L{i + 1}")[:5] if i < len(cfg["launch"]) else f"L{i + 1}")
    labels.update(names)
    labels[keys.KEY_INFO] = "INFO"
    return out, labels


def _per_key(var, actions):
    """duckyScript running actions[k] when `var` == k, for each k."""
    lines = []
    for k, action in actions.items():
        lines += [f"IF {var} == {k}"] + action.split("\n") + ["END_IF"]
    return lines



def slot_title_script(key):
    """The title screen, run when slot `key` is let go after a press.

    The press script has already told the daemon (press hotkey) and waited for it
    to write that session's title and status into _GV0.._GV26 and then _GV27 = the
    key. A script sees the GVs as they were when it started (writes that land while
    it runs are stored but invisible to it), so this script never waits on new data.
    It keeps the screen up and handles every key itself while it's showing:

    - same key again: send its "open" hotkey and close
    - another session key: close (tap it again to see that session; typing its
      hotkey from here and closing while it was still held left Ctrl stuck down)
    - any other key: do what that key does and close (INFO just closes)
    - nothing for TITLE_SHOW_MS: close

    Every key that ends the screen is waited on until it's let go, and the script
    ends by releasing every modifier, so the pad never leaves one held.

    The firmware reports the key that started a script as still pressed, so a key
    only counts when the reading changes (starting from this key), and a key that
    ends the screen is waited on (up to 1 s) until it's let go. Scripts never write _GVs (the
    firmware refuses that). The text is _GV0.._GV26 copied to scratch memory and
    printed a row at a time with PUTS (bit 29 = print to the OLED); see oled.py.
    """
    base, cols = oled.BASE, oled.COLS
    loading = ["OLED_CLEAR", "OLED_CURSOR 0 4", "OLED_PRINT loading...", "OLED_UPDATE"]
    draw = ["OLED_CLEAR"] + [f"POKE32({base + 4 * i}, _GV{i})" for i in range(oled.SCREEN_GVS)]
    draw.append(f"POKE8({base + 4 * oled.SCREEN_GVS}, 0)")
    for r in range(oled.SCREEN_ROWS):
        y = 4 + 14 * r + (6 if r >= oled.ROWS else 0)        # a gap between title and status
        draw += [f"OLED_CURSOR 0 {y}", f"PUTS({base + cols * r} | ({cols} << 16) | (1 << 29))"]
    draw += ["OLED_CURSOR 0 114", "OLED_CPRINT tap again to open", "OLED_UPDATE"]
    open_ = {k: keys.HOTKEY_FOR[keys.Event("open", k)].ducky() for k in keys.SLOT_KEYS}
    other = {ev.key: hk.ducky() for ev, hk in keys.HOTKEY_FOR.items()
             if ev.kind == "press" and keys.slot_of(ev.key) is None}
    other[keys.ENC2_CW], other[keys.ENC2_CCW] = "MK_VOLUP", "MK_VOLDOWN"
    # One flat loop: no inner WHILE and no LBREAK. An LBREAK right after an inner
    # WHILE didn't leave the screen on the pad (it looped forever, or kept sending
    # "open"), so closing is a state: wait for the key to be let go, then end.
    end = ["closing = 1", "t0 = _TIME_MS"]
    lines = [f"VAR want = {key}", "VAR shown = 0", f"VAR last = {key}", "VAR start = _TIME_MS",
             "VAR t0 = 0", "VAR p = 0", "VAR closing = 0", "VAR done = 0"] + loading
    lines += [f"WHILE done == 0 && (closing == 1 || _TIME_MS - start < {TITLE_SHOW_MS})",
              "IF closing == 1",
              "IF _READKEY == 0 || _TIME_MS - t0 > 1000",   # let go (or gave up waiting)
              "done = 1",
              "END_IF",
              "ELSE",
              f"IF _GV{oled.GV_HANDSHAKE} == want && shown != want"] + draw + ["shown = want", "END_IF",
              "p = _READKEY",
              "IF p != last",                                # act on a change only: the key that
              "last = p",                                    # started this script reads as still down
              "IF p != 0",
              "IF p == want"] + _per_key("want", open_) + ["END_IF"]
    lines += _per_key("p", other) + end + ["END_IF", "END_IF", "END_IF", "DELAY 20", "END_WHILE", "OLED_RESTORE"]
    lines += [f"KEYUP {m}" for m in ("CTRL", "ALT", "SHIFT", "COMMAND")]   # never leave a modifier held
    return "\n".join(lines)


TITLE_SHOW_MS = 4000
TITLE_WAIT_MS = 120          # press script: time for the daemon to store the title

INFO_SCRIPT = """\
// devinducky summary. The daemon packs a byte per value:
// _GV28 = most urgent | waiting << 8 | working << 16 | done << 24
//   most urgent: 0 none 1 ended 2 idle 3 done 4 working 5 starting 6 error 7 waiting 8 approval
// _GV29 = api (0 ok 1 auth 2 net) | sessions on keys << 8
// The firmware redraws the key labels as soon as a script that drew to the
// OLED ends, so stay running for a few seconds (or until another key) first.
VAR waiting = (_GV28 >> 8) & 255
VAR working = (_GV28 >> 16) & 255
VAR done = (_GV28 >> 24) & 255
VAR api = _GV29 & 255
VAR count = (_GV29 >> 8) & 255
OLED_CLEAR
OLED_CURSOR 0 10
OLED_CPRINT devinducky
OLED_CURSOR 0 40
IF api == 1
    OLED_CPRINT API KEY ERROR
ELSE IF api == 2
    OLED_CPRINT API UNREACHABLE
ELSE IF count == 0
    OLED_CPRINT no sessions yet
ELSE IF waiting > 0
    OLED_CPRINT $waiting WAITING ON YOU
ELSE IF working > 0
    OLED_CPRINT $working working
ELSE
    OLED_CPRINT all quiet
END_IF
OLED_CURSOR 0 70
OLED_CPRINT sessions $count  done $done
OLED_CURSOR 0 110
OLED_CPRINT any key to close
OLED_UPDATE
VAR start = _TIME_MS
WHILE _TIME_MS - start < 5000
    VAR k = _READKEY
    IF k != 0 && k != 20
        LBREAK
    END_IF
    DELAY 20
END_WHILE
OLED_RESTORE
"""


PROFILE_NAME = "devinducky"
ENCODER_LABELS = {keys.ENC1_CW: "SEL+", keys.ENC1_CCW: "SEL-", keys.ENC1_PUSH: "OPEN",
                  keys.ENC2_CW: "VOL+", keys.ENC2_CCW: "VOL-", keys.ENC2_PUSH: "REFR"}


def build_profile(cfg, out_dir):
    """Write profile_<name>/ in the configurator's on-disk format; return its path.

    Format (duckyPad-Configurator duck_objs.py): config.txt names each key with
    `zN <label>` (a key without a z line isn't loaded), `dr N` turns off
    auto-repeat, `SWCOLOR_N r g b` is the key's idle colour; scripts live in
    keyN.txt (on press) and keyN-release.txt (on release).
    """
    scripts, labels = profile_scripts(cfg)
    labels.update(ENCODER_LABELS)
    pdir = os.path.join(out_dir, f"profile_{PROFILE_NAME}")
    shutil.rmtree(pdir, ignore_errors=True)
    os.makedirs(pdir)
    lines = []
    for k in range(1, 27):
        press, release = scripts.get((k, "press"), ""), scripts.get((k, "release"), "")
        lines.append(f"z{k} {labels.get(k, '-')}")
        lines.append(f"dr {k}")          # holding must not re-fire the script
        for name, body in ((f"key{k}.txt", press), (f"key{k}-release.txt", release)):
            if body:
                with open(os.path.join(pdir, name), "w", newline="\n") as f:
                    f.write(body if body.endswith("\n") else body + "\n")
    for k in range(1, 21):
        lines.append(f"SWCOLOR_{k} 0 0 0")
    lines += ["BG_COLOR 0 0 0", "DIM_UNUSED_KEYS 0"]
    with open(os.path.join(pdir, "config.txt"), "w", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return pdir


def cmd_gen_profile(args):
    import zipfile
    os.makedirs(args.out, exist_ok=True)
    pdir = build_profile(config.load(), args.out)
    zpath = os.path.join(args.out, f"duckyPad_Profile_{PROFILE_NAME}.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(pdir)):
            z.write(os.path.join(pdir, name), f"{os.path.basename(pdir)}/{name}")
    print(f"wrote {zpath}")
    print("Configurator: Import → pick this zip → Save. Then close the configurator.")


def _service():
    return f"gui/{os.getuid()}/{LABEL}"


def _launchd_pid():
    """PID of the launchd-managed daemon, 0 if loaded but not running, None if not loaded."""
    out = subprocess.run(["launchctl", "print", _service()], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        if line.strip().startswith("pid = "):
            return int(line.split("=")[1])
    return 0


def _stray_daemons(exclude):
    """Other `devinducky run` processes (e.g. one left running in a terminal)."""
    out = subprocess.run(["pgrep", "-f", "devinducky run"], capture_output=True, text=True)
    pids = {int(p) for p in out.stdout.split()} - {os.getpid(), exclude or -1}
    # `uv run` wraps the real process; keep only processes whose parent isn't also listed.
    def ppid(pid):
        r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
        return int(r.stdout.strip() or 0)
    return sorted(p for p in pids if ppid(p) not in pids and ppid(p) != exclude)


def _write_plist():
    exe = shutil.which("devinducky") or os.path.join(os.path.dirname(sys.executable), "devinducky")
    os.makedirs(config.HOME, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [exe, "run", "--log-file"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardErrorPath": os.path.join(config.HOME, "launchd.err"),
        "EnvironmentVariables": {"SHELL": os.environ.get("SHELL", "/bin/zsh")},
    }
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    return exe


def cmd_up(args):
    exe = _write_plist()
    if _launchd_pid() is None:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST], check=True)
    # Start it now (RunAtLoad alone can leave a fresh bootstrap idle); -k restarts a running one.
    subprocess.run(["launchctl", "kickstart", "-k", _service()], check=True)
    for _ in range(20):
        pid = _launchd_pid()
        if pid:
            break
        time.sleep(0.1)
    if not pid:
        sys.exit(f"devinducky didn't start; see {os.path.join(config.HOME, 'launchd.err')}")
    print(f"devinducky is up (pid {pid}), starts at login. Logs: devinducky logs")
    stray = _stray_daemons(pid)
    if stray:
        print(f"warning: another devinducky is running outside launchd (pid {', '.join(map(str, stray))}); "
              "stop it or every key press will act twice.")
    py = os.path.realpath(sys.executable)
    print(f"If pad keys do nothing, grant Accessibility to {py}\n"
          f"  (System Settings › Privacy & Security › Accessibility, + then Cmd-Shift-G to paste the path).")


def cmd_down(args):
    was = _launchd_pid()
    subprocess.run(["launchctl", "bootout", _service()], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("devinducky is down." if was is not None else "devinducky wasn't running under launchd.")
    stray = _stray_daemons(None)
    if stray:
        print(f"note: devinducky still running outside launchd (pid {', '.join(map(str, stray))}).")


def cmd_status(args):
    pid = _launchd_pid()
    if pid is None:
        print("down")
    elif pid == 0:
        print(f"loaded but not running (crashed?); see {os.path.join(config.HOME, 'launchd.err')}")
    else:
        print(f"up (pid {pid})")
    if os.path.exists(config.LOG_FILE):
        with open(config.LOG_FILE) as f:
            tail = f.readlines()[-5:]
        print("".join("  " + l for l in tail), end="")


def cmd_logs(args):
    os.execvp("tail", ["tail", "-n", "40", "-F", config.LOG_FILE])


def main(argv=None):
    p = argparse.ArgumentParser(prog="devinducky")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the daemon in the foreground")
    r.add_argument("-v", "--verbose", action="store_true")
    r.add_argument("--dry-pad", action="store_true", help="don't touch the pad; log frames instead")
    r.add_argument("--log-file", action="store_true", help=f"log to {config.LOG_FILE}")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("test-pad", help="cycle LEDs and echo key events").set_defaults(fn=cmd_test_pad)
    sub.add_parser("recents", help="show which session each slot key holds").set_defaults(fn=cmd_recents)
    sub.add_parser("playbooks", help="list org playbooks").set_defaults(fn=cmd_playbooks)
    g = sub.add_parser("gen-profile", help="write the duckyPad profile scripts")
    g.add_argument("--out", default="profile")
    g.set_defaults(fn=cmd_gen_profile)
    sub.add_parser("up", aliases=["install", "restart"],
                   help="run in the background (launchd; restarts if running, starts at login)").set_defaults(fn=cmd_up)
    sub.add_parser("down", aliases=["uninstall"], help="stop the background daemon").set_defaults(fn=cmd_down)
    sub.add_parser("status", help="is the background daemon running?").set_defaults(fn=cmd_status)
    sub.add_parser("logs", help="follow the daemon log").set_defaults(fn=cmd_logs)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
