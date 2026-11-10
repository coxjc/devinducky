# devinducky

Watch and drive [Devin](https://docs.devin.ai/api-reference/overview) sessions from a [duckyPad Pro](https://github.com/duckyPad/duckyPad-Pro).
The twelve session keys mirror the recent-sessions list in the Devin desktop app: each key's LED shows that session's state, a tap shows its title and status on the pad's screen, and a second tap opens it in the app.
Inspired by [claude-pad](https://github.com/URL42/claude-pad).

This fork works with the **Devin desktop app only**; nothing opens in a browser. macOS only.

## Layout

```
 ┌──────┬──────┬──────┬──────┐
 │ S1   │ S2   │ S3   │ S4   │  your 12 most recent sessions, in the app sidebar's order
 ├──────┼──────┼──────┼──────┤    tap           = show the session's title and status on the screen
 │ S5   │ S6   │ S7   │ S8   │    tap it again  = open it in the Devin app (while its title shows)
 ├──────┼──────┼──────┼──────┤    LED           = session state
 │ S9   │ S10  │ S11  │ S12  │
 ├──────┼──────┼──────┼──────┤
 │ L1   │ L2   │ L3   │ L4   │  launch presets: tap to arm (blinks), tap again within 3 s to launch
 ├──────┼──────┼──────┼──────┤
 │ NEW  │ DASH │ REFR │ INFO │  Devin app · Devin app · refresh now · OLED summary
 └──────┴──────┴──────┴──────┘
 upper encoder: rotate = step through sessions (the app follows when you stop), push = open
 lower encoder: rotate = system volume, push = refresh
```

**Which sessions.** The app's sidebar lists your sessions by the time of their last message, yours or Devin's, and leaves out archived ones. devinducky ranks them the same way, so S1 is the top of the sidebar. The keys reorder as the list does: message an older session and it moves to S1. Sessions whose origin is in `hide_origins` (by default `webapp`: scheduled and auto-fix sessions, which the sidebar doesn't list) are skipped. `devinducky recents` prints what each key holds right now.

**The title screen.** A tap shows the session's title (up to 4 lines) and its status: "Waiting for you", "Needs approval", "Working", "Done", "Asleep" or the error, the number of PRs, and when it's waiting on you, the start of Devin's last message. While it's up, every key still works: tap the same key again to open the session, tap another session key to see that one instead, and any other key does its usual job and closes the screen. It closes by itself after 4 seconds.

**Opening.** devinducky opens a session by clicking its row in the Devin app's sidebar (found by session ID, through Accessibility), launching the app first if needed. The app's own `devin://` links only open sessions that belong to one of the app's spaces and send everything else to the browser, so the link is only a fallback for a session that isn't in the sidebar. The log says which was used ("via sidebar" or "via link"). Launched sessions open in the app too.

**How text reaches the pad.** The pad can't receive text, only numbers, so on every session-key press the daemon packs the title and status into the pad's global variables `_GV0`–`_GV26` and then sets `_GV27` to the key. The title screen, which keeps running while it's shown, draws them once `_GV27` names the key it's waiting for.

INFO shows a summary on the OLED for 5 seconds, or until you press another key. The summary includes the API status, how many sessions are waiting on you or working, and how many keys hold a session.

| LED | Meaning |
|---|---|
| amber, pulsing | waiting for you |
| orange, fast pulse | waiting for approval |
| blue | working |
| cyan | starting / resuming |
| green (dims after 30 min) | finished |
| red | error, or suspended for limits / credits |
| soft pink | asleep (suspended for inactivity, or you paused it) |
| very dim | exited or archived |
| off | no session (fewer than 12) |

Notifications fire for the sessions on the keys when one starts waiting, finishes, or errors.

## How it works

The duckyPad can't report key presses over HID. Each key's script therefore types a unique hotkey, such as `Ctrl+Alt+F13`, and the daemon listens for these with `pynput`. A session key types one hotkey when pressed ("show my title"); its title screen, which runs on the pad, types an "open" hotkey when the same key is tapped again.

In the other direction, the daemon drives the pad over HID. It sets each key's LED and writes global variables (`_GV20`–`_GV27`) that the INFO key's script reads. It polls the Devin v3 API for sessions updated since the last poll and reads a session's messages only when it may have moved into the top 12.

`gen-profile` builds the pad profile from the same key table the daemon uses, so the two can't drift apart.

## Setup

1. Put a Devin **Personal Access Token** in your shell profile: `export DEVIN_API_KEY=…`.
   - The daemon reads it from the environment. Under launchd it reads it from your login shell instead.
   - The key is never written to disk.
2. `uv tool install --editable .` puts `devinducky` on your PATH (in `~/.local/bin`). The command runs this checkout, so code changes apply after `devinducky restart`.
3. `devinducky gen-profile` writes `profile/duckyPad_Profile_devinducky.zip`. In the [duckyPad configurator](https://github.com/duckyPad/duckyPad-Configurator):
   1. **Import** the zip.
   2. **Save** to the pad.
   3. **Close the configurator.** It holds the device while it's open.
   - When you re-import, delete the old profile first, because Import adds a new copy.
   - Re-run `gen-profile` after renaming launch presets.
4. Grant **Accessibility** to your terminal in System Settings › Privacy & Security. Without it, no key presses are seen.
5. `devinducky test-pad` cycles the LEDs and prints key down/up events with timings.
6. `devinducky recents` prints the session each key will hold.
7. `devinducky up` starts the daemon in the background. Use `devinducky run -v` instead to run it in the foreground while debugging. The first time a session opens, macOS asks to allow **Automation** for the Devin app.

### Running in the background

```sh
devinducky up        # start (or restart) under launchd; also starts at login and restarts if it dies
devinducky status    # running? plus the last few log lines
devinducky logs      # follow ~/.devinducky/daemon.log
devinducky restart   # after changing code or config.toml
devinducky down      # stop, and don't start at login
```

Under launchd, the process that needs Accessibility is the real Python binary, not your terminal. `up` prints its path; add it in System Settings. `up` also warns if a foreground `devinducky run` is still going, because with two daemons every key press acts twice.

## Config: `~/.devinducky/config.toml`

Every key is optional. The defaults are in [`src/devinducky/config.py`](src/devinducky/config.py).

```toml
poll_s = 10
hide_origins = ["webapp"]           # session origins the app sidebar doesn't list
pad_profile = 0                     # only paint when this pad profile number is active (0 = any)
notify = { waiting = true, finished = true, error = true }   # macOS banners
ntfy_url = ""                       # optionally also POST notifications here

[colors]
working = [0, 60, 255]

[[launch]]                          # L1..L4, in order
name   = "Fix CI"                   # first 5 chars become the key label
prompt = "Look at the latest failing CI run on main in org/repo and open a PR fixing it."
repos  = ["org/repo"]
color  = [120, 0, 255]
# also: playbook_id, title, devin_mode, max_acu_limit, tags
```

`devinducky playbooks` lists your org's playbook IDs. Launched sessions are tagged `devinducky`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Keys do nothing | Is the daemon running? Does the right process have Accessibility? Is the pad on the devinducky profile? Check with `test-pad`. |
| LEDs never change | Close the duckyPad configurator. |
| Tap shows "loading..." | The daemon isn't running or can't reach the pad. Check `devinducky status`. |
| Keys are in a different order than the app | Run `devinducky recents` and compare; REFR polls now. Sessions with a hidden origin are skipped. |
| Second tap doesn't open the session | Allow Automation for the Devin app in System Settings › Privacy & Security › Automation. If it opens in the browser, the session isn't in the app's sidebar. |
| First press after a while does nothing | The pad was asleep and that press woke it. Press again. |
| INFO says API KEY ERROR | `DEVIN_API_KEY` is missing or invalid. Under launchd, make sure your login shell exports it. |

## Development

`uv run pytest`. The tests need no pad or network.

Hardware details are in [`hid.py`](src/devinducky/hid.py): VID `0x0483`, PID `0xD11D`, and the protocol from the [duckyPad HID docs](https://github.com/duckyPad/duckyPad-Profile-Autoswitcher/blob/master/HID_details.md). On macOS the device is opened non-exclusively, so other tools can share it.

## License

[MIT](LICENSE)
