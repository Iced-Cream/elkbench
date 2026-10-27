# elklock: one owner per Elektron unit

Every AI agent session on a PC (any client, any folder) and the person share the units on USB.
elklock gives one of them a temporary claim on a unit, so two sessions never flash, press keys,
take screenshots or record audio on the same unit at once. Stdlib Python, Windows and Linux.
A session is named by `ELKBENCH_SESSION_ID` (else Claude Code's `CLAUDE_CODE_SESSION_ID`), its
client process by `ELKBENCH_CLIENT_PID` (else `CLAUDE_PID`).
Part of elkbench (whose daemon imports it), but it runs as its own process so claims survive a
daemon restart. It was called devlock; the hook still accepts `devlock.py` in a command line.

```
set D=C:\src\elkbench\elklock\elklock.py
python %D% status                                         # holders, queue, last handoff
python %D% claim rytm --for "store check"                 # now, or BUSY (exit 3)
python %D% claim rytm --for "store check" --wait --timeout 10m   # queue for it
python %D% run rytm -- python -m elkflash upgrade X.syx --port Rytm --yes --no-confirm
python %D% renew rytm --max 2h
python %D% release rytm --note "left on stock 1.74, project unchanged"
python %D% claim a4 --manual me --for "Transfer" --max 4h  # the person
python %D% break rytm --yes                               # the person frees a stuck claim
```

## How the lock works
- **The lock is a Windows file handle.** `claim` starts a detached holder process that opens
  `%LOCALAPPDATA%\elklock\<device>.lock` with no sharing and keeps it open; any other open of
  that file fails with a sharing violation, which is "busy". Windows closes the handle whenever
  the holder exits, so a claim cannot outlive it.
- `<device>.json` beside the lock names the holder (session id, folder, purpose, running
  commands); it is a label, not the lock. `ELKLOCK_DIR` overrides the folder (tests use a scratch dir).
- Devices: `rytm`, `a4`, or `all`. Claiming several is all-or-nothing.

## When a claim ends
| Event | Ends the claim |
|---|---|
| `release` | at once (within ~1 s), unless a `run` command is still going: then `release` refuses |
| the claiming session's client process exits | within ~1 s, **after** any running `run` command ends |
| `--max` reached (default 2 h, `renew` extends) | yes, but never during a `run` command or a listening test |
| `--idle` with no device use (default 15 min) | yes, but never during a `run` command or a listening test. Use = `run`, `renew`, or a device command the hook let through |
| a listening test (`listen DEVICE --for "what"` until `heard DEVICE --by "where"`) | never: `release` refuses and the hook denies every device command meanwhile; only the person ends it (the hook denies `heard` from a session: elkbench's listen_done asks the person) |

A claim that ends by itself (idle, max time, session gone) records the OS and the screen in
its handoff note, as `release` does.
| `break --yes` (the person) | at once; kills the holder if it does not let go |

## Safety additions
- **`run`**: a command under `run` keeps the claim alive whatever the timers say, and the claim
  ends ~1 s after it finishes if a timer has passed meanwhile. The hook refuses `elkflash upgrade`
  unless it runs under `elklock run`.
- **`claim --wait`**: a ticket in `queue\<device>\`, served oldest first; a plain claim never jumps
  the queue. Tickets of exited sessions or timed-out waits drop out. Handoff to the next in line
  took ~1 s in tests.
- **Idle release**: a finished session that forgets to release frees the unit after 15 min.
- **Handoff log**: `release` records the OS version and a screenshot in one read-only call
  (`elkflash info --screenshot`, picture in `handoff\<device>\screen.png`), for the Rytm the
  active project's kit 1 name (`analogrun.py active`, with any note a local write policy adds),
  and your `--note` in `<device>.handoff.json`; the next `claim` and `status`
  print it. `--no-snapshot` skips the device reads. An automatic end records only why it ended.
  The snapshot uses `tools.elkflash` and `tools.analoghealth` from elkbench's config.toml (an
  installed elkflash, and no active-project read, without them).

## The hook (enforcement)
`elklock.py hook` is a PreToolUse hook for Bash and PowerShell in Claude Code's JSON format (a
client with its own pre-command hooks can call it the same way). It denies a command
that runs a device tool (analogusb, analogrun, elkflash, obtr, and elkbench's audio recorders
asio_rec, asio_tone, asio_loop, wavein_rec, loopback.py; more from `[elklock] guard` in elkbench's config.toml) unless this session holds the claim on
the unit the command names (Rytm, Analog Four/A4), and denies `elkflash upgrade`
outside `elklock run`. Offline forms (selftest, --self-test, decode, encode, bootstrap, list,
--dry-run, unit tests) and emulator-only ports (rytm-emu, a4-emu) pass. With no unit
named, any claim held by the session is enough. It matches a tool's name as a word of its
own (`analogrun`, `tools\analogrun.py`; not `test_analogusb.py`), and splits the command
line at `;`, `&&`, `||`, `|` and `&` outside quotes, keeping a heredoc with its command. It skips
the parts that are file, text and git commands (`cat`, `cp`, `ls`, `grep`, `sed`, `awk`, `echo`,
`cd`, `git`, `Get-Content`, `Copy-Item` and the like), but still checks what they run inside
`$(...)`, backticks or `find -exec`. So reading a run's `elkflash.log`, editing `analogusb.py`
with `sed` or committing with a message that names a tool passes; any other command that only
mentions a tool name is refused too.
It is a guard rail for well-behaved sessions, not security: every session runs as the same
Windows user and could edit the label or kill a holder.

In `~/.claude/settings.json`:
```json
"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell", "hooks": [{"type": "command",
  "command": "python C:/src/elkbench/elklock/elklock.py hook"}]}]}
```

## Tested (fake sessions, scratch lock folder, no unit touched)
Busy and all-or-nothing claims; a raw open of a held lock file is refused by Windows; crash of the
claiming client process frees the unit within ~1 s; idle release; a `run` command holding the claim
past both idle and max, `release` refused during it, unit free 1.0 s after it ended; queue of two
waiters served in order, each 1.0 s after the release before it, with a plain claim refused
behind them; handoff note shown to the next holder; hook decisions.

## The desktop pane (`elklock-pane/`)
A Claude Code mod that shows, beside the chat, each unit as free or held (by which session, for
what, idle and time left), any running `elklock run` command, the queue and the last handoff;
it re-reads `elklock.py status --json` every 3 s and opens at session start (`/elklock` reopens
it). Its one setting, `python`, is the Python that runs elklock.py (default `python` on PATH).
To load it in every session, `~/.claude/settings.json` needs
`"env": {"CLAUDE_CODE_PLUGIN_DIRS": "C:/src/elkbench/elklock/elklock-pane"}`; removing that
entry turns it off.
