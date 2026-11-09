"""The Devin desktop app (a Windsurf/VS Code build): bring it forward and show its cloud sessions."""

import logging
import subprocess
import time

log = logging.getLogger(__name__)

APP = "Devin"
BUNDLE_ID = "com.exafunction.windsurf"

# The app only finds a session from the link once it has loaded its session list;
# otherwise it hands the link to the default browser. Bringing it forward first and
# giving it a moment avoids that; a cold start needs longer.
SETTLE_S = 0.5
LAUNCH_SETTLE_S = 3.0
LAUNCH_TIMEOUT_S = 10.0


class DesktopError(Exception):
    pass


def osascript(script, timeout=5):
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise DesktopError(out.stderr.strip() or "osascript failed")
    return out.stdout.strip()


def running():
    return osascript(f'application id "{BUNDLE_ID}" is running') == "true"


def session_link(sid):
    return f"devin://acp/session?sessionId=devin-{sid}&connectorId=devin-cloud"


def activate():
    """Bring the app forward, launching it if needed. Returns True if it had to launch."""
    launched = not running()
    if launched:
        subprocess.run(["open", "-b", BUNDLE_ID], capture_output=True)
        deadline = time.monotonic() + LAUNCH_TIMEOUT_S
        while not running():
            if time.monotonic() > deadline:
                raise DesktopError(f"{APP} didn't start")
            time.sleep(0.2)
    osascript(f'tell application id "{BUNDLE_ID}" to activate')
    return launched


def open_session(sid):
    """Show cloud session `sid` in the desktop app. Returns False if that couldn't be done."""
    try:
        launched = activate()
    except (DesktopError, subprocess.TimeoutExpired) as e:
        log.warning("desktop open %s: %s", sid, e)
        return False
    time.sleep(LAUNCH_SETTLE_S if launched else SETTLE_S)
    subprocess.run(["open", "-b", BUNDLE_ID, session_link(sid)], capture_output=True)
    return True
