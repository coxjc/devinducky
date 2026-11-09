"""Session titles for the pad's OLED, packed into its global variables.

The pad can't receive text over HID, only 32-bit globals. A title is wrapped to
ROWS x COLS characters, space padded to 80 bytes and packed little-endian into
_GV0.._GV19. The slot keys' release script copies those into scratch memory at
BASE and prints each row with PUTS (see cli.slot_release_script).
"""

import struct
import textwrap

ROWS, COLS = 4, 18            # OLED font is 7x10 px: 18 characters per line
TITLE_GVS = 20                # _GV0.._GV19 = 80 bytes
BASE = 0xF400                 # scratch memory the pad script copies the GVs into


def title_rows(title):
    """`title` word-wrapped to at most ROWS rows of COLS ASCII characters; '...' marks a cut."""
    text = title.encode("ascii", "replace").decode("ascii")
    rows = textwrap.wrap(text, COLS) or [""]
    if len(rows) > ROWS:
        last = rows[ROWS - 1]
        rows = rows[:ROWS - 1] + [(last if len(last) + 3 <= COLS else last[:COLS - 3].rstrip()) + "..."]
    return rows


def title_gvs(title):
    """{gv_index: value} for _GV0.._GV19 holding `title`."""
    data = "".join(r.ljust(COLS) for r in title_rows(title)).encode("ascii").ljust(4 * TITLE_GVS, b" ")
    return {i: struct.unpack_from("<I", data, 4 * i)[0] for i in range(TITLE_GVS)}
