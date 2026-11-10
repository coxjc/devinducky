"""A session's title and status for the pad's OLED, packed into its global variables.

The pad can't receive text over HID, only 32-bit globals. The title is wrapped to
ROWS rows and the status to STATUS_ROWS rows of COLS characters, space padded and
packed little-endian into _GV0.._GV26 (108 bytes). The slot keys' title screen
copies those into scratch memory at BASE and prints each row with PUTS (see
cli.slot_title_script).
"""

import struct
import textwrap

ROWS, COLS = 4, 18            # OLED font is 7x10 px: 18 characters per line
STATUS_ROWS = 2
SCREEN_ROWS = ROWS + STATUS_ROWS
SCREEN_GVS = SCREEN_ROWS * COLS // 4   # _GV0.._GV26 = 108 bytes
BASE = 0xF400                 # scratch memory the pad script copies the GVs into
GV_HANDSHAKE = 27             # the slot key whose screen is in _GV0.._GV26 (written last)


def title_rows(title, rows=ROWS):
    """`title` word-wrapped to at most `rows` rows of COLS ASCII characters; '...' marks a cut."""
    text = title.encode("ascii", "replace").decode("ascii")
    out = textwrap.wrap(text, COLS) or [""]
    if len(out) > rows:
        last = out[rows - 1]
        out = out[:rows - 1] + [(last if len(last) + 3 <= COLS else last[:COLS - 3].rstrip()) + "..."]
    return out


def screen_gvs(title, status=""):
    """{gv_index: value} for _GV0.._GV26 holding `title` (4 rows) then `status` (2 rows)."""
    rows = (title_rows(title) + [""] * ROWS)[:ROWS] + (title_rows(status, STATUS_ROWS) + [""] * STATUS_ROWS)[:STATUS_ROWS]
    data = "".join(r.ljust(COLS) for r in rows).encode("ascii")
    return {i: struct.unpack_from("<I", data, 4 * i)[0] for i in range(SCREEN_GVS)}
