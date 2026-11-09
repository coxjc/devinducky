"""Your sessions in the order the Devin desktop app's recent-sessions sidebar shows them.

The sidebar sorts by the time of each session's last message (yours or Devin's)
and hides archived sessions. The API can't sort that way, so this keeps the
last-message time of every session it has seen and re-reads a session's
messages only when its `updated_at` moves (a new message always moves it).

A session's last message is never newer than its `updated_at`, so until its
messages are read `updated_at` stands in as an upper bound, and messages are read
only for sessions that bound places in the top `n`.

The first refresh lists your newest sessions; later ones ask only for sessions
updated since the previous refresh, so a months-old session you message today
still jumps to the top.
"""

import time

from . import state

OVERLAP_S = 120          # re-ask for a little before the last refresh, for clock skew


class Recents:
    def __init__(self, devin, n=12, hide_origins=("webapp",), first=100):
        self.devin = devin
        self.n = n
        self.hide_origins = set(hide_origins)
        self.first = first
        self.sessions = {}       # session_id -> latest session dict (visible ones only)
        self.last_msg = {}       # session_id -> (updated_at, last message ts or bound, exact?)
        self.synced_at = None    # wall time the last successful refresh started

    def refresh(self):
        """Fetch what changed and return the top `n` session dicts, most recent first."""
        started = time.time()
        if self.synced_at is None:
            fetched = self.devin.my_sessions(first=self.first)
        else:
            fetched = self.devin.my_sessions(first=self.first, updated_after=int(self.synced_at - OVERLAP_S))
        for s in fetched:
            self._take(s)
        while True:
            pending = [sid for sid in self._ranked()[:self.n] if not self.last_msg[sid][2]]
            if not pending:
                break
            for sid in pending:
                self._read_messages(sid)
        self.synced_at = started
        return self.top()

    def _take(self, s):
        sid = s["session_id"]
        if s.get("is_archived") or s.get("origin") in self.hide_origins:
            self.sessions.pop(sid, None)
            self.last_msg.pop(sid, None)
            return
        self.sessions[sid] = s
        cached = self.last_msg.get(sid)
        if cached is None or cached[0] != s.get("updated_at"):
            self.last_msg[sid] = (s.get("updated_at"), state.parse_ts(s.get("updated_at")), False)

    def _read_messages(self, sid):
        s = self.sessions[sid]
        times = [state.parse_ts(m.get("created_at")) for m in self.devin.session_messages(sid)]
        times = [t for t in times if t is not None]
        self.last_msg[sid] = (s.get("updated_at"), max(times) if times else state.parse_ts(s.get("created_at")), True)

    def _ranked(self):
        def key(sid):
            return (self.last_msg[sid][1] or 0, state.parse_ts(self.sessions[sid].get("updated_at")) or 0)
        return sorted(self.sessions, key=key, reverse=True)

    def top(self):
        return [self.sessions[sid] for sid in self._ranked()[:self.n]]
