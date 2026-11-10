"""Which session each key shows: sticky, so a session keeps its key once it has one.

The keys hold your NUM_SLOTS most recent sessions (Recents' ranking), but they
aren't re-sorted when that ranking changes. A session already on a key stays
there. A session that newly makes the top NUM_SLOTS takes an empty key, or the
key of a session that dropped out of the top (which has been quiet longer than
every session still in it). Archived sessions drop out, freeing their key.

The assignment is saved to slots.json so a restart doesn't reshuffle the pad.
"""

import json
import os
import tempfile


def assign(current, ranked):
    """New key assignment: `current` is [session_id | None] per key, `ranked` the top
    session ids, most recent first (at most len(current) of them)."""
    top = set(ranked)
    kept = [sid if sid in top else None for sid in current]
    newcomers = iter(sid for sid in ranked if sid not in kept)
    return [sid if sid is not None else next(newcomers, None) for sid in kept]


def load(path, n):
    try:
        with open(path) as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return ([s if isinstance(s, str) and s else None for s in raw] + [None] * n)[:n]
    except (FileNotFoundError, ValueError):
        pass
    return [None] * n


def save(path, slots):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(slots, f)
    os.replace(tmp, path)
