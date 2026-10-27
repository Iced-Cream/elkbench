# elkbench: Elektron hardware tests as one local MCP server

elkbench gives every AI agent session on a PC (any MCP client: Claude Code, or another LLM's
agent) the same small set of tools for testing
Elektron units (Analog Rytm MKII, Analog Four MKII, Digitone, and Digitakt, which is untested on
hardware) and their emulators: claim a unit,
run a test plan, flash an OS, take screenshots, record and check USB audio, and take or restore
an Overbridge Total Recall image. It enforces the rules that keep several sessions from
tripping over each other or over the person at the unit: one owner per unit, emulator first,
and a hardware run only after the person says go.

Stdlib Python, Windows and Linux (see Install). Bundled here: `elklock\` (per-unit claims, plus a hook and a
desktop pane for Claude Code), `audio\` (USB audio recording and checks) and `obtr.py` (Total Recall through
the Overbridge Engine).

## Shape
- **One daemon per PC** (`daemon.py`): owns the units' MIDI ports, ASIO and the Overbridge
  Engine connection, one user per port at a time. It listens on 127.0.0.1 (port and secret in
  `%LOCALAPPDATA%\elkbench\daemon.json`), holds `daemon.lock` so a second copy exits, restarts
  itself for newer code on disk (`daemon.py`, `batch.py`, `plancheck.py`, `elklock.py`) only once elklock has
  every unit free (no claim, queued claim or listening test), no go is open and nothing of its own
  is going (run, approval, emulator, another call), so sessions land elkbench code without
  checking claims; code that does not import stays unused (`status` says why), and it exits after
  30 min idle.
- **One launcher per session** (`launcher.py`): the MCP stdio server the client starts. It
  forwards each call with the session id and the pid of the client process (a claim ends when
  that process exits), and starts the daemon when none runs. A client sets them with
  `ELKBENCH_SESSION_ID` and `ELKBENCH_CLIENT_PID` in the server's environment; Claude Code needs
  nothing (its `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PID`, else the nearest `claude*` ancestor).
  Without either, the session is `mcp-<launcher pid>` and its claims end by release, idle or
  max time only.
- It **wraps** analogrun.py / testplan.py (analoghealth), elkflash, the audio scripts in
  `audio\` and obtr.py; it does not change them. Claims go through elklock with the session's
  own identity, so the elklock pane and the Bash hook (kept as a backstop) see the same claim.

## Configuration
Nothing about a folder layout is built in. `%LOCALAPPDATA%\elkbench\config.toml` (or
`ELKBENCH_CONFIG`) names the tools (analoghealth, elkflash, the emulators, a Python for the
audio checks), where each unit's evidence and reference screens go, which OS files count as
stock, and the person named in the approval dialog. Every key is optional: a tool left out is
off and its MCP tools say which key to set; evidence defaults to `Documents\elkbench\<unit>\runs`.
See `config.example.toml`.

## Tools (each answers in a few lines: `ok:`, `pass:`, `fail:`, `refused:`, `running:`, `waiting:`)
| tool | what | needs |
|---|---|---|
| claim / release | elklock claim (prints the last handoff) / release with a note | release writes the handoff note (note + runs + OS left) and a row in the unit's `test-log.md`; refuses a non-stock OS unless `accept_non_stock`. Where the unit's config sets `clear_at_release` (Rytm, A4), release first backs up each project in those slots into the session's evidence folder, erases it (`data clear`, through the write policy) and lists the projects to show them empty; if it can't, the claim is kept unless `accept_slots_left` (the handoff note then says NOT EMPTY). The go request names that cleanup |
| status | this session's claims and runs, or one `run_id` | |
| emulator | start / stop / status of the emulators: one per session, headless, USB MIDI over its own TCP port (analog-emu `--usb-midi-tcp`, digiemu's `tools/usbrun.py`; analogrun, digirun and elkflash take `--port tcp:HOST:PORT`), on a fresh card copy; an analog-emu with `--din-midi-tcp` also gets its DIN MIDI on a TCP port of its own | an analog-emu build with `--usb-midi-tcp`; for the Digitone a digiemu with USB MIDI |
| approve_plan | one go for a whole hardware run: plans, flashes, recall restores | each item's emulator pass (a flash item: of the file over its `over` OS, else the file this session flashed last), and each plan's start runs (built-in starts, contrasts); the person's yes in the client, or on the popup fallback (No by default, 30 min timeout) |
| approval_text | the go request approve_plan would make for the same arguments, word for word, and its `text_id`; asks nobody, and refuses now if approve_plan would. Post it in the thread, then pass the `text_id` to approve_plan, which refuses if the request has changed since | as approve_plan |
| check_plan | the plan checker (`plancheck.py`) on one or more hardware plans, in seconds and with no emulator: the errors approve_plan refuses on, the flags the go request shows, and whether a go would be accepted now (an emulator pass of the current steps, plans sharing a reference file). Rules and templates: [docs/testing-practices.md](docs/testing-practices.md), "Plan checker and templates", and `templates/` | an analoghealth checkout (it parses the plans) |
| unit_state | what is known about a unit without touching it, each with its age: the claim now, the last handoff note (OS, active project), the last project listing a hardware `data ls /projects` saw, and the last test-log row | nothing |
| answer_in_thread | the person's reply in the session's chat (a thread or channel) to an approval whose client prompt could not be shown | `approval_fallback` thread or both, after a fast No; the reply's id or link |
| end_go | ends a go nobody will use (the session's own or one another session left), so it stops holding off a restart for newer code; with `request`, withdraws the session's own go request still waiting; with neither, lists the open gos and waiting requests. approve_plan refuses while the session has a go open or a request waiting, so one ask cannot leave two gos | refused while the go's session has a hardware run going |
| listen / listen_done | a listening test: the person listens to the unit; until listen_done the claim cannot time out and nothing may drive the unit. A plan's `listen TEXT` step does the same mid-run (the run waits; `status` says so); an emulator run skips it and names it in its result, and the go request lists it as not tested in the emulator. Not yet tried on Digitone or Digitakt hardware | claim; listen_done: the id of the person's "finished" reply in the thread, else Claude asks them |
| run_plan | `analogrun --quiet run PLAN` in the background, returns a run id; its result names the run's log and screenshots. In the emulator (Rytm, A4) it reads every saved project before and after the plan and fails the run if one outside the slots the plan saves into changed | emulator: nothing (a twin with absolute paths is written for you); hardware: claim, emulator pass, approval token |
| flash | `elkflash upgrade --no-confirm --wait` in the background; its result keeps the sent, Committed and "back after" lines from elkflash's log | hardware: claim, a passing emulator flash of the same file over the same OS (`over`, else the file this session flashed last), token |
| batch | emulator only (every unit; the Digitone and Digitakt rows below give their limits): a plan or flash run `runs` times per variant (each variant names the OS it boots and its image or plan), each run on a fresh emulator of its own, `parallel` at a time (all batches on the PC share `batch_max_emulators`, default 8), round by round in an order shuffled by a reported seed. `status` gives progress, then per variant its pass count with a 95% Wilson range and Fisher's exact p against the first variant (Holm-adjusted when there are several), and the first failures. `results.jsonl`, `summary.txt` and the folders of runs that did not pass go in `batch-<run id>` in the evidence folder. Snapshots are off by default (a resumed snapshot starts every run at the same emulated moment); `snapshot` on a plan batch with `snapshot-after` takes each variant's setup snapshot once, or reuses a stored one, and every run resumes it (in one six-step plan, 89 s a run instead of 146 s). No early stop, never an emulator pass. **Gate batches** (`gate`, `plans`: up to 16 hardware plans) are the exception: each plan runs once, side by side, each on a fresh emulator of the given build (resuming its setup snapshot when it has `snapshot-after`; plans with the same setup share one), with run_plan's saved-project check, and a passing run is recorded as that plan's emulator pass, its evidence in the plan's own runs folder. A plan that names its OS (`os =`) boots that file instead, so plans on different OS files gate together. Each plan also runs from every start its go needs (the unit's built-in `dirty` and `occupied`, and the starts its contrasts name), each on a fresh emulator, and each run that passes, or stops where the plan's contrast says, is recorded. Plans and start runs whose result still stands (same steps, refs and OS files, same build booted, from a fresh emulator or a setup snapshot) are kept and not run again, unless `force` | nothing |
| steps / shot / expect_screen | analogrun `do` on the unit or emulator, into the session's evidence folder | hardware: claim; steps up to 2 min; `send` only carries Elektron dumps (in a plan too: an absolute path, checked and part of what an emulator pass vouches for, or a file an earlier `capture` saved); `capture NAME S [any\|none]` saves the SysEx that came back since the last `send`, written even when nothing came; `ping` reads the unit's product and OS (on the Digitone and Digitakt both need a digirun that has them, digihealth's `runner-capture-ping`; with an older one they are refused; on those two, capture and ping through elkbench are untried on hardware). A trailing `din` (`send FILE din`, `capture NAME S [any\|none] din`) sends into or listens on the unit's DIN MIDI instead of USB, each port with its own "since the last send" (Rytm and A4 only; the write policy is still asked over USB): on hardware through the interface named by `din` in the unit's config section, which must be connected; in the emulator through analog-emu's `--din-midi-tcp` (refused on a build without it). Steps, plans and batches pass analogrun `--din` only when a step uses it |
| (Digitone) | steps, shot and expect_screen through digihealth's `digirun.py` (`tools.digihealth`, a digihealth tree with the runner), on the unit or its emulator. Keys need a digihealth build; screens work on stock. The emulator is digiemu (`digiemu_usb.py` sets a build up once in a digiemu home, then boots a copy per session), and `flash` with `build`, or with a Digitone emulator running, upgrades it over USB with elkflash; without one, `flash target emulator` runs digiemu's `emu.fwcheck`. Plans (`unit = digitone`) run through testplan, which hands their steps to digirun in a process of its own. `send` and `save-project` ask the same local write policy (a policy can tell a Digitone session by `session.unit`; there a test project is marked by its pattern A01's name); there is no `save-kit`. Batches run on digiemu too, at most 3 at a time, without setup snapshots | hardware: claim |
| (Digitakt) | **Untested on hardware.** The Digitone's path, through digirun and digiemu: steps, shot, expect_screen, data, plans, `flash` and flash or plan batches, on the unit or in digiemu (OS 1.54; it needs a digiemu that resolves the Digitakt mk1 USB symbols, as the `elkbench-support` branch does). Every Digitakt flash sends 4 KB chunks (`--chunk-size 4096`): in digiemu an upgrade in elkflash's default 30 KB chunks stalls before the OS commits it, and 4 KB ones go through. So a Digitakt plan cannot `flash` (testplan sends the default chunks): flash with the `flash` tool, then run the plan. digihealth turns encoders on the Digitone only, so `turn` is refused; no `audio_record` (no Overbridge channel order known, and digiemu's stream was silent) and no Total Recall (obtr knows no Digitakt state types). digirun calls every session `digitone`, so a write policy marks a Digitakt test project by pattern A01 as well | hardware: claim |
| data | the data store: `ls PATH` (`/` gives the roots) and `get PATH` (one object as Transfer stores it), through analogrun or digirun `data`, into the evidence folder; `put FILE` into PATH, `clear PATH` and `roundtrip PATH [to]` through analogrun `data` on every unit (the data store RPC is the same on the Digitone, where reads and writes have run only in the emulator so far, untried on hardware; on the Digitakt untried), so the local write policy is asked first, and a copy of what is replaced goes to the evidence folder | hardware: claim |
| audio_record / audio_check | asio_tone / asio_rec into the evidence folder / audiocheck verdict | record: claim; check: offline |
| recall_take / recall_restore | obtr.py through the running Overbridge Engine | claims on all three units (the Engine is shared); restore also a token. Never starts or restarts Overbridge |

## Practices
How to run hardware tests safely, what elkbench enforces and what is left to the session:
[docs/testing-practices.md](docs/testing-practices.md). An example write policy that keeps
tests away from the owner's own projects (a `~T` name prefix, reserved slots 121-128) is in
`examples/write-policy/`.

## Rules it enforces
- **Claim**: every hardware call checks elklock (`mine` for this session) first; it never breaks
  a claim. Emulator and offline calls need none.
- **Write policy**: writes on a unit go through analogrun, so a local write policy installed
  for analoghealth (its `writepolicy.py`) applies to them; with `require_write_policy = true`
  hardware tools refuse while none is installed. Evidence goes to the unit's
  evidence folder, in `mcp-<date>-<session>`.
- **Emulator first**: an emulator pass is recorded (in `emulator-passes.json`) only for a
  passing run without `record_refs`; it is keyed by unit, steps, reference screens (sha256),
  the OS file it flashes and the OS it runs on (`os = FILE`, sha256), so a changed plan, ref or
  build needs a new emulator pass. A plan with `os =` runs only on an emulator booted on that
  file, and on hardware is refused when this session left another OS on the unit; a flash
  plan must name it (the plan checker refuses one without). A flash's own pass is keyed by the
  file and the OS the emulator ran before it, so the hardware `flash` (and each `flashes` item
  of a go) names the OS on the unit with `over`, or uses the file this session flashed last.
  Passes recorded before the key named the OS still count where the build they booted was
  that OS (a plan pass records it; a Digitone/Digitakt firmware check counts over any OS). The plan's
  own `emulator-run` gate (testplan.py) still applies to flash plans. A pass also records the
  build the emulator booted and how the run started (a fresh emulator, a setup snapshot, or the
  session's emulator as earlier calls left it); the go request shows the start.
- **`power-cycle`**: a plan step on a line of its own (Rytm and A4). In the emulator, the plan's
  emulator starts with analog-emu's `--power-off`; at the step elkbench sends it `quit`, so the
  OS runs its power-fail save and the card image is written, checks the emulator's log for the
  save, and cold-boots it on that card and flash (without the setup snapshot it may have
  resumed). On the unit the run waits at the step, as at a `listen` step, until the person has
  power cycled it by hand and said so (`listen_done`); the go request lists each one. elkbench
  runs the plan in parts between the power cycles, each with its own run folder, and the pass
  is keyed by the plan as written. Each power cycle needs a machine assertion after it (the
  readback of what it had to keep), and a plan with one cannot flash, put it in a `(hardware)`
  section or in the setup (`snapshot-after`). Batches do not power cycle yet: the plan's own
  pass comes from `run_plan`, and a gate batch runs only its start runs (the runs from the
  starts its go needs), each up to its first power cycle, so every one of them must stop, as a
  contrast says, in a section before that power cycle. A counting batch refuses such a plan.
  When the plan reads back state that earlier plans set on the same emulator, start that
  emulator with `power_off` (emulator tool) and run the plan without `build`. The analog-emu
  build needs `--power-off` (its `--help` does not list it; elkbench looks for its power-off log
  line in the binary).
- **`wait N audio`**: a wait that only lets the unit make sound (a recording, a tone, a pause to
  hear something). The runner waits N s on the unit; every emulator twin (run_plan, batch)
  waits 0.5 s instead, as the emulator makes no voice sound. The pass is keyed by the plan as
  written, and run results and the go request list each shortened wait.
- **One go per run**: `approve_plan` asks the person in the client (MCP elicitation: the
  launcher sends `elicitation/create` and relays the answer as the daemon's `answer` method),
  and falls back when the client cannot show it: an error, or a No or cancel within 2 s (a
  remote session such as Claude Code's Remote Control declines in about 40 ms without showing anything; `clients.jsonl` logs
  each answer and how fast it came). A client without elicitation gets the popup on the PC. After
  a fast No, `approval_fallback` in config.toml picks the route: `popup` (the default), `thread`
  (the session posts the request in the chat it reports to and passes the person's reply on with
  `answer_in_thread`, with the reply's id or link, kept in the log; no answer in 30 min is a No)
  or `both` (the first answer counts). The thread route is weaker than the popup, since the
  daemon cannot tell who wrote the reply. Tokens exist only in the daemon's memory, are bound to the session, cover the
  listed items once each and expire after 2 h. Guard rail, not security: every session runs as
  the same Windows user, and a fake launcher could relay a yes; only the popup is out of a
  session's reach. `clients.jsonl` records which clients can ask.
- **Listening tests**: while the person listens (elklock `listen` ... `heard`), every hardware
  call to that unit is refused, the elklock hook denies device commands, release waits, and
  the claim cannot end by idle or max time. Only the person ends it: `listen_done` with their
  thread reply's id, or their answer to the prompt; the hook denies `elklock heard` from a session.
- **Back to the emulator gives the unit back**: an emulator call (target emulator, or starting
  an emulator) from a session that holds units releases them first, with a handoff note,
  unless one of its hardware runs is going or the person is listening. Claim again for the next
  hardware run: emulator work comes first, then the claim.
- **Ask first, claim after the yes**: approve_plan refuses while the session holds a unit idle
  (no hardware run of its own going, nobody listening), so no unit sits blocked while the
  person decides. The go lasts 2 h; the session claims (queueing if need be) after the yes and
  checks the unit's state before writing. A claim that ends by itself records the OS and the
  screen in its handoff note.
- **Flash plans on hardware**: run_plan writes a hardware twin with `emulator-run =` the
  recorded emulator pass, so testplan's flash gate passes without editing the plan; a pass
  whose OS file differs is refused before the person is asked.
- **Stock OS last**: release asks for it; a hardware flash records stock or not by sha256
  against the unit's `stock` files.
- **No `stats` on remote-only builds**: a plan or steps call with a `stats` step is refused when
  the OS (the plan's flash, the unit's last flash or the emulator's build) is one of the unit's
  `no_stats` files.
- **Plan checker**: approve_plan refuses a hardware plan with no start check (an `expect-screen`
  in its first section, before anything is written), a section that doesn't say what it proves
  (`say proves setup|set-and-kept|works|evidence: how`), a `set-and-kept` or `works` section
  with no machine assertion, a `listen` without `allowed:` and `needs:` or without a screen check
  before it, key or read steps after a stock flash, anything but NO after a `save-before-load`
  check, and plans in one go that compare the same
  reference file. `check_plan` gives the same answer before any emulator time.
- **Messy starts and contrast runs**: a hardware plan's go also needs a run from each built-in
  start its unit has among `dirty` and `occupied` (`starts/<unit>/`, so far the Rytm MKII; not for
  a plan that flashes), and
  from each start its `say contrast START: why` lines name. Each run passes, or stops in the
  section whose contrast names its start (a check seen to fail). A gate batch runs them all, a
  built-in start built once per build as a snapshot. A pass on the session's emulator records what
  ran on it before (the go request lists it), and is not recorded after an ungated write (an
  emulator-only plan that writes, a `steps` or `data` write). See
  [docs/testing-practices.md](docs/testing-practices.md), "Messy starts and contrast runs".
- **No sleep mid-run**: while a unit is claimed or queued for, or a run, an emulator, a go or its
  dialog is going, the daemon keeps the PC from sleeping (Windows: SetThreadExecutionState;
  Linux: systemd-inhibit, when installed) and lets it sleep again once all of that is over, so
  a flash, a listening test or a remote client session isn't cut off. The display may still
  turn off. `keep_awake = false` in config.toml turns this off; `calls.jsonl` logs each change.
- **Plan format**: `format = 1` (missing counts as 1); other versions are refused.
- **Log**: every call in `%LOCALAPPDATA%\elkbench\calls.jsonl` with the session id; run output
  in `jobs\<run id>.log`.

## Install
Needs Windows or Linux (see Linux below) and Python 3.11 or newer (stdlib only; `tomllib`).
The tools it wraps are separate: an analoghealth checkout for plans, steps and screens, elkflash (a checkout, or
`pip install`ed) for flashing, an analog-emu build with `--usb-midi-tcp` for the emulators.
For the Digitone and Digitakt, digihealth (`tools.digihealth`, for digirun) and digiemu
(`tools.digiemu`) come from the `elkbench-support` branches of
[Iced-Cream/digihealth](https://github.com/Iced-Cream/digihealth/tree/elkbench-support) and
[Iced-Cream/digiemu](https://github.com/Iced-Cream/digiemu/tree/elkbench-support) until their
pull requests are merged upstream (irpina/digihealth, irpina/digiemu).
The audio scripts are bundled in `audio\` (see its README); `audio_check` needs a Python with
numpy and scipy (`tools.audio_python`), and the optional picture tools there need Pillow.
1. Clone this repo anywhere, say `C:\src\elkbench`.
2. Copy `config.example.toml` to `%LOCALAPPDATA%\elkbench\config.toml` and set the tools you
   have. Leave out the rest; their MCP tools say which key turns them on.
3. Register the MCP server in your client as a stdio server that runs
   `python C:\src\elkbench\launcher.py`. For Claude Code, for every session:
   ```
   claude mcp add --scope user elkbench -- python C:\src\elkbench\launcher.py
   ```
   Other clients: set `ELKBENCH_SESSION_ID` (unique per session) and `ELKBENCH_CLIENT_PID` in
   the server's environment if they can (see Shape); a client without MCP elicitation gets the
   approval popup on the PC.
4. Optional, Claude Code: the elklock backstop and pane (see `elklock\README.md`): in `~/.claude/settings.json`,
   ```json
   "env": {"CLAUDE_CODE_PLUGIN_DIRS": "C:/src/elkbench/elklock/elklock-pane"},
   "hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell", "hooks": [{"type": "command",
     "command": "python C:/src/elkbench/elklock/elklock.py hook"}]}]}
   ```
5. Optional, Claude Code: a cheap test runner: copy `agents\elkbench-runner.md` to `~/.claude/agents\`. It is
   a Haiku subagent with the test tools only: it runs plans, flashes and steps and reports the
   tool lines verbatim, while the session that starts it claims, approves, releases and reads
   the results. A plan run costs it about 9k tokens. With another client, a small model with the
   same brief (the file's body) does the same job.
6. Check it: `python test_elkbench.py` (offline; see Tests).

### Linux
The same steps, with Linux paths: the config is `~/.config/elkbench/config.toml` and the state
`~/.local/state/elkbench` (elklock's locks in `~/.local/state/elklock`). Differences:
- **Audio** goes through PortAudio: `pip install sounddevice numpy` (plus scipy for
  `audio_check`). Class-compliant USB audio gives the unit's main outputs; there are no
  per-track outputs without Overbridge. `asio_loop`, `wavein_rec` and `loopback` stay Windows only.
- **Total Recall** is off: Elektron makes the Overbridge Engine for Windows and macOS only.
- **The emulators**: an analog-emu build for Linux (`rytm`, `a4`); digiemu runs directly
  (`tools.digiemu_python` is just its Python).
- **MIDI**: analoghealth and elkflash use mido / python-rtmidi there (`pip install mido
  python-rtmidi`).

Tested on Linux (WSL, 2026-10-24): the offline tests, elklock's lock tests, and a Rytm emulator
reached over TCP from Linux. Not yet tried on Linux: a unit on USB, audio from a unit. macOS
may work (the same POSIX code) but is untested.

## To do
- Make the MCP the only way in for sessions: once it covers the data store, kit dumps and
  Digitone keys and plans, the elklock Bash hook refuses raw device commands from sessions and
  points to these tools. elklock stays its own process (claims survive a daemon restart;
  `--manual`, the pane and the hook use it).

## Tests
`python test_elkbench.py`: scratch lock, state and config folders and the sample plans in
`tests\` (stand-in reference screens and OS files are written to the scratch folder), no unit
touched. The plan tests need an analoghealth checkout (`ELKBENCH_TEST_ANALOGHEALTH`, or
`tools.analoghealth` in your config) and are skipped without one. They cover MCP over stdio,
two sessions and a busy claim, the token's one use and session binding, a dialog No, emulator
twins sharing the key, release asking for stock and writing the handoff and log, steps rules,
the emulator-flash gate and the Engine needing all three claims.
`python test_plancheck.py`: the plan checker's rules and the templates in `templates/`, with no
analoghealth checkout needed.
`audio\`: `python -m unittest test_audiocheck` there.

## Licence

GPL-2.0-or-later. elkbench is free software: you can redistribute it
and/or modify it under the terms of the GNU General Public License as
published by the Free Software Foundation, either version 2 of the License, or
(at your option) any later version. It is distributed in the hope that it will
be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
License for more details: [LICENSE](LICENSE) holds version 2.

Not affiliated with Elektron. Analog Rytm, Analog Four, Digitone, Digitakt and Overbridge are
trademarks of Elektron.
