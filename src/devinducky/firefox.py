"""Firefox tabs through the macOS Accessibility API.

Firefox has no AppleScript dictionary for windows and tabs, so the Chromium and
Safari scripts in browser.py don't work for it. Its accessibility tree does expose
what we need: the page's URL on the AXWebArea in the focused window, and each tab
as an AXRadioButton (subrole AXTabButton) titled with the page title. Tab buttons
carry no URL, so focusing a session's tab goes by its page title, which Devin sets
to "<session title> - Devin".

Reading the tree needs the same Accessibility permission the daemon already needs
for key presses.
"""

import logging

log = logging.getLogger(__name__)

BUNDLE_IDS = {
    "Firefox": "org.mozilla.firefox",
    "Firefox Developer Edition": "org.mozilla.firefoxdeveloperedition",
    "Firefox Nightly": "org.mozilla.nightly",
}
PAGE_TITLE_SUFFIX = " - Devin"
_AX_API_DISABLED = -25211


class FirefoxError(Exception):
    pass


def _ax():
    import ApplicationServices
    return ApplicationServices


def _app(name):
    """(NSRunningApplication, AXUIElement) for a running Firefox, else (None, None)."""
    from AppKit import NSRunningApplication
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(BUNDLE_IDS[name])
    if not apps:
        return None, None
    return apps[0], _ax().AXUIElementCreateApplication(apps[0].processIdentifier())


def _attr(el, name):
    err, value = _ax().AXUIElementCopyAttributeValue(el, name, None)
    if err == _AX_API_DISABLED:
        raise FirefoxError("Accessibility access isn't granted to this process")
    return value if err == 0 else None


def _find(el, match, max_depth=25):
    """Elements under `el` (not inside web content) for which `match` is true, depth first."""
    out = []

    def walk(e, depth):
        if match(e):
            out.append(e)
        if depth >= max_depth or _attr(e, "AXRole") == "AXWebArea":
            return
        for child in _attr(e, "AXChildren") or []:
            walk(child, depth + 1)

    walk(el, 0)
    return out


def active_url(name):
    """URL of the selected tab in Firefox `name`'s focused window."""
    _, app = _app(name)
    if app is None:
        raise FirefoxError(f"{name} isn't running")
    win = _attr(app, "AXFocusedWindow") or _attr(app, "AXMainWindow")
    if win is None:
        raise FirefoxError(f"{name} has no open window")
    areas = _find(win, lambda e: _attr(e, "AXRole") == "AXWebArea")
    url = _attr(areas[0], "AXURL") if areas else None
    if url is None:
        raise FirefoxError(f"no page in {name}'s front window")
    return str(url.absoluteString()) if hasattr(url, "absoluteString") else str(url)


def tab_title(session_title):
    return f"{session_title}{PAGE_TITLE_SUFFIX}"


def focus_tab_titled(name, title):
    """Select the tab titled `title` in Firefox `name` and bring its window forward.

    Returns False if Firefox isn't running or no tab has that title.
    """
    ns_app, app = _app(name)
    if app is None:
        return False
    ax = _ax()
    for win in _attr(app, "AXWindows") or []:
        tabs = _find(win, lambda e: _attr(e, "AXSubrole") == "AXTabButton" and _attr(e, "AXTitle") == title)
        if not tabs:
            continue
        ax.AXUIElementPerformAction(tabs[0], "AXPress")
        ax.AXUIElementPerformAction(win, "AXRaise")
        ns_app.activate() if hasattr(ns_app, "activate") else ns_app.activateWithOptions_(0)
        return True
    return False
