"""Read the active browser tab's URL via JXA (osascript) and pull out a Devin session ID."""

import json
import logging
import re
import subprocess

log = logging.getLogger(__name__)

# Scripts are JXA, not AppleScript. With several instances of one browser running (e.g.
# chrome-devtools-mcp or a Puppeteer profile alongside your own Chrome), AppleScript's
# `tell application "Google Chrome"` can land on a headless automation instance that has
# no windows ("Can't get window 1"); JXA's Application("Google Chrome") reaches the real one.
_CHROMIUM = 'Application({app}).windows[0].activeTab.url()'
_SAFARI = 'Application({app}).documents[0].url()'
SCRIPTS = {
    "Google Chrome": _CHROMIUM, "Arc": _CHROMIUM, "Brave Browser": _CHROMIUM,
    "Microsoft Edge": _CHROMIUM, "Chromium": _CHROMIUM,
    "Safari": _SAFARI,
}


def _q(s):
    return json.dumps(s)


class BrowserError(Exception):
    pass


def _osascript(script, timeout=5, jxa=False):
    cmd = ["osascript"] + (["-l", "JavaScript"] if jxa else []) + ["-e", script]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise BrowserError(out.stderr.strip() or "osascript failed")
    return out.stdout.strip()


def frontmost_app():
    return _osascript('tell application "System Events" to get name of first application process '
                      'whose frontmost is true')


def running(app):
    # Ask System Events for a live process: LaunchServices' `application "Safari" is running`
    # can say true while Safari isn't actually up, and the next call then fails with -600.
    return _osascript(f'tell application "System Events" to exists process "{app}"') == "true"


def active_tab_url(browsers):
    """URL of the active tab in the frontmost supported browser, else the first running one."""
    try:
        front = frontmost_app()
    except BrowserError:
        front = None
    order = ([front] if front in browsers else []) + [b for b in browsers if b != front]
    errors = []
    for app in order:
        script = SCRIPTS.get(app)
        if not script:
            continue
        try:
            if app != front and not running(app):
                continue
            return app, _osascript(script.format(app=_q(app)), jxa=True)
        except (BrowserError, subprocess.TimeoutExpired) as e:
            errors.append(f"{app}: {e}")
    raise BrowserError("; ".join(errors) or "no supported browser is running")


# One Apple Event fetches every tab URL (windows.tabs.url() -> nested list); the loops
# below run locally, which is ~5x faster than asking the browser tab by tab.
_LIST_TABS = '''
const u = Application({app}).windows.tabs.url();
const out = [];
u.forEach((wl, wi) => wl.forEach((url, ti) => out.push((wi + 1) + "\\t" + (ti + 1) + "\\t" + url)));
out.join("\\n");
'''
_FOCUS = {
    "chromium": 'const a = Application({app}); const w = a.windows[{w} - 1];\n'
                'w.activeTabIndex = {t}; w.index = 1; a.activate();',
    "Arc": 'const a = Application({app}); a.windows[{w} - 1].tabs[{t} - 1].select(); a.activate();',
    "Safari": 'const a = Application({app}); const w = a.windows[{w} - 1];\n'
              'w.currentTab = w.tabs[{t} - 1]; w.index = 1; a.activate();',
}


def parse_tab_list(out):
    """'w<TAB>t<TAB>url' lines -> [(window, tab, url)]."""
    tabs = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            tabs.append((int(parts[0]), int(parts[1]), parts[2]))
    return tabs


def focus_tab(browsers, needle):
    """Bring an already-open tab whose URL contains `needle` to the front.

    Only browsers that are already running are searched (so nothing gets launched).
    Returns the browser name if a tab was focused, else None.
    """
    for app in browsers:
        if app not in SCRIPTS:
            continue
        try:
            if not running(app):
                continue
            tabs = parse_tab_list(_osascript(_LIST_TABS.replace("{app}", _q(app)), jxa=True))
            hit = next(((w, t) for w, t, url in tabs if needle in url), None)
            if not hit:
                continue
            tpl = _FOCUS.get(app, _FOCUS["chromium"])
            _osascript(tpl.replace("{app}", _q(app)).replace("{w}", str(hit[0])).replace("{t}", str(hit[1])),
                      jxa=True)
            return app
        except (BrowserError, subprocess.TimeoutExpired) as e:
            log.warning("focus_tab %s: %s", app, e)
            continue
    return None


def session_id_from_url(url, pattern):
    """Extract the session ID in the bare form the API returns as `session_id`.

    The API accepts both `devin-<hex>` and `<hex>` in paths but reports the bare
    one, so we store that to keep bindings and poll results comparable.
    """
    m = re.search(pattern, url or "")
    if not m:
        return None
    return m.group(1).removeprefix("devin-")
