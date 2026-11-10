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
    """Show cloud session `sid` in the desktop app. Returns how ("sidebar" or "link"), or None.

    The app's own link handler shows a session only if it's already assigned to
    one of the app's spaces; anything else (sessions started from Slack, Linear,
    the API) goes to the browser. Clicking the session's row in the app's sidebar
    always opens it in the app, so do that, and fall back to the link only when
    the row can't be found.
    """
    try:
        launched = activate()
    except (DesktopError, subprocess.TimeoutExpired) as e:
        log.warning("desktop open %s: %s", sid, e)
        return None
    deadline = time.monotonic() + (LAUNCH_SETTLE_S if launched else SETTLE_S)
    while True:
        try:
            if click_sidebar_session(sid):
                return "sidebar"
        except (DesktopError, subprocess.TimeoutExpired, ValueError) as e:
            log.info("sidebar click for %s failed: %s", sid, e)
            break
        if time.monotonic() > deadline:
            break
        time.sleep(0.2)
    subprocess.run(["open", "-b", BUNDLE_ID, session_link(sid)], capture_output=True)
    return "link"


def _pid():
    out = osascript('tell application "System Events" to get unix id of first application process '
                    f'whose bundle identifier is "{BUNDLE_ID}"')
    return int(out)


def _sidebar_row(app, sid, ax):
    """The sidebar link for session `sid` (its URL ends /sessions/<sid>), else None."""
    suffix = f"/sessions/{sid}"

    def attr(el, name):
        err, value = ax.AXUIElementCopyAttributeValue(el, name, None)
        return value if err == 0 else None

    def walk(el, depth):
        if attr(el, "AXRole") == "AXLink":
            url = attr(el, "AXURL")
            if url is not None and str(url).rstrip("/").endswith(suffix):
                return el
        if depth >= 45:
            return None
        for child in attr(el, "AXChildren") or []:
            hit = walk(child, depth + 1)
            if hit is not None:
                return hit
        return None

    for win in attr(app, "AXWindows") or []:
        hit = walk(win, 0)
        if hit is not None:
            return hit, attr
    return None, attr


def click_sidebar_session(sid):
    """Click session `sid`'s row in the app's sidebar. Returns False if there's no such row.

    Electron ignores the Accessibility "press" action on these rows, so this posts
    a real mouse click on the row and puts the pointer back where it was. Needs
    the Accessibility permission the daemon already has.
    """
    import ApplicationServices as ax
    import Quartz
    app = ax.AXUIElementCreateApplication(_pid())
    row, attr = _sidebar_row(app, sid, ax)
    if row is None:
        return False
    ax.AXUIElementPerformAction(row, "AXScrollToVisible")
    time.sleep(0.3)                                   # let the list settle before reading where the row is
    _, pos = ax.AXValueGetValue(attr(row, "AXPosition"), ax.kAXValueCGPointType, None)
    _, size = ax.AXValueGetValue(attr(row, "AXSize"), ax.kAXValueCGSizeType, None)
    point = (pos.x + min(size.width / 2, 60), pos.y + size.height / 2)
    home = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                           Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft))
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, home, 0))
    return True
