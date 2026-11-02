# Testing on hardware: practices

How to run tests on an Elektron unit (Analog Rytm MKII, Analog Four MKII, Digitone, Digitakt) with
elkbench, whether a person or an AI agent session drives them. elkbench enforces some of these
rules itself; each section says which ones. The rest is up to the session, and a session that
uses elkbench is expected to follow all of them.

The aim is a run that leaves the unit as it found it, never touches the owner's own work, and
reaches the hardware only with something that has already worked in the emulator.

## 1. The emulator comes first

- **Every build is tested in the emulator before a unit sees it**: the emulator flash and
  boot, then the test steps themselves, with a pass. The hardware run then repeats the full
  test. It checks everything the emulator showed and adds what the emulator can't assess
  (analog voices, real USB audio, the panel, the real flash part). A step that fails in the
  emulator is not tried on the unit.
- **The gate is per flash, not per build.** Every flash in the run is run in the emulator as
  the same transition, in the same order: what is on the unit, then what goes on. That covers
  build-to-build steps and the restore to stock over the last build. A transition passes when
  the upgrade completes: the flash is erased and written, the unit reboots, and the new OS
  version is checked. A build that flashes onto stock says nothing about whether it can be
  flashed away from.
- **Only the gate's own emulator passes count.** A go lists passes recorded by the tool that
  will run the hardware steps, for the same plan file, reference screens and OS file. An
  emulator run elsewhere (another machine, by hand) is a pre-check. A plan changed after its
  pass needs a new one.
- **A build for key-driven plans carries the test harness.** If the plans press keys remotely
  and that needs a harness mod in the firmware, the test build is the mods under test plus the
  harness from the start, and the plan lists the harness apart from the mods under test.
- **A gate step that can't run is a failure, not a skip.** If one emulator can't do a step,
  use another, or name the gap in the plan and get the owner's explicit yes for that step.
  That yes is part of the run's go and covers that run only.
- **A power cycle is a plan step** (`power-cycle`) where the emulator can run the OS's power-off
  save (analog-emu's or digiemu's `--power-off`): the emulator pass then covers a real power-off save and a
  cold boot on that card, and on the unit the run pauses at the step until the owner says the
  power cycle by hand is done. A readback after it proves what it had to keep, and stops the run
  if it doesn't. A pass from an emulator that never lost power proves only "kept with nothing
  happening".
  Batches don't power cycle yet: such a plan's contrasts belong in sections before its first
  power cycle (a gate batch runs its start runs up to there), and its own pass comes from
  `run_plan`, on an emulator started with `power_off` when earlier plans set up its state.
- **A power cycle by hand outside a plan** is for units or OSes the emulator can't power off.
  It may run ungated when the go names it as such, every other step of the run has its
  emulator pass, the run pauses until the owner says it is done, and the next plan starts with a
  readback that proves the state the power cycle had to keep (and stops the run if it doesn't).
  Never power cycle by software.
- Every test plan ends with the restore to stock, and that flash passes in the emulator too.
- **The emulator's card is not the unit's.** Each emulator run starts from a fresh copy of its
  card, so its project slots say nothing about the unit's. A plan handles the slot it writes as
  it finds it on the unit at run time (empty, or a test item to clear first), and its emulator
  run fills that slot first so the clearing path is tested too.
- **Nor is its working state.** The active project's kit, pattern, tempo and unsaved changes,
  and the UI's memory (the last subpage a page key opened, menu cursors), differ between the
  unit and the emulator. A plan sets every state it depends on itself, reaches pages through a
  known page, enters menus from their top, proves each precondition on screen, and is gated
  from unit-like starts (the built-in `dirty` and `occupied` starts, see "Messy starts and
  contrast runs"). Global settings it relies on, such
  as MIDI channels, get a readback before anything depends on them.
- **A plan makes all the state it needs, inside its own claim.** Treat everything on the unit
  at claim time as unknown: what the test slots hold, the active project, its unsaved changes,
  whoever left them. Never build a plan, or a go, on state an earlier run or another session
  left on the unit. The setup creates the test project itself and sets every value the checks
  use. When a later run needs what an earlier run made, the earlier run copies it into its run
  folder and the later run writes it back from that file into a test slot, then proves it.
  A precondition proves something the plan's own setup produced, not a name the plan's own
  save step has just written.
- **A reference screen compares only what the plan controls.** A screen recorded in the
  emulator can show card content the plan doesn't own: other slots in a project list, other
  kits, sounds or samples, settings. On the unit those differ, and a check that compares them
  fails on a correct screen. Limit the check to the rows of the plan's own slot or item, or to
  the prompt or field it tests. Before recording, check the compared rows against what would
  differ on the unit: list the unit's projects read only (`data ls`) when checking the plan,
  before the go is asked for, and it shows which rows of a list screen will.

*elkbench enforces*: a hardware plan or flash needs a passing emulator run of the same steps,
reference screens and OS file (sha256). A plan that flashes gets that emulator run named for
it, and a pass whose OS file differs is refused before the owner is asked. An emulator run of a
plan (on every unit) reads every saved project before and after the steps (read only, through the
data store), and fails if one was added, removed, renamed or changed outside the slots the plan
saves into. Going back to the
emulator gives the unit back (section 4).

### Fast emulator gates

- **Each plan's pass starts from a known state.** A plan that passed right after another plan
  on the same emulator only shows that its steps pass from whatever that plan left. Gate the
  plans of a run together in a gate batch (`batch` with `gate`): every plan runs once on a fresh
  emulator of the build, side by side, so several plans take about as long as the slowest one.
- **The setup goes inside each plan**, not in a separate setup plan run first. Mark where it
  ends with `snapshot-after = SECTION`: the setup runs once per build, its end state is kept as a
  snapshot, and each plan resumes it. Plans whose setup sections are the same share one
  snapshot, so copy the setup from a template rather than rewording it per plan. A pass then
  covers the plan's own setup, and the unit runs the same setup in full.
- **Waits that only let the unit make sound are marked `wait N audio`.** The emulator makes no
  voice sound, so it waits half a second there; the unit waits the full N seconds. Use it only
  where nothing on the screen or in the data changes during the wait.
- **Re-gate only what changed.** A gate batch leaves out a plan whose pass still stands: the same
  steps, reference screens and OS files, on the same build, from a fresh emulator or a setup
  snapshot. Changing a plan's setup changes the plan, so every plan with that setup runs again.

*elkbench enforces*: a gate batch records a pass only for a hardware plan that passed on its own
fresh emulator (or its setup snapshot), with the saved-project check above, and keeps its
evidence with the plan's other runs. A pass records the build it booted and how it started,
and the go request shows that start and lists every `wait N audio` that was shortened. A plan
that names its OS (`os = FILE`) runs only on an emulator booted on that file, its pass is keyed
with it, and a flash's pass is keyed with the OS it flashed over: stock back over build A does
not vouch for stock back over build B.

### Messy starts and contrast runs

A pass from a fresh emulator says the plan works from the card as shipped. A unit is never in
that state, and a check that passes from every state proves nothing. Two kinds of extra runs
close those gaps.

- **Built-in starts.** elkbench ships start plans per unit (`starts/<unit>/`, emulator only)
  that leave a fresh emulator the way a unit is usually found. For the Rytm MKII:
  - `dirty`: the card's own project active, another pattern and kit, an unsaved change, and
    the LFO page left open;
  - `testslot`: a test project in slot 121 active, with an unsaved change;
  - `occupied`: every test slot 121-128 holding a throwaway test project;
  - `testslot-cleared`: `testslot`, then slot 121 cleared through the data store, as a run's
    backup and clear leaves a unit (the active-project marker stays on the empty slot).
  A start can build on another (`base = START`) and run data store steps (`data = clear PATH;
  put PATH FILE`) before its own sections, so it can reach states the front panel cannot
  ([starts/README.md](../starts/README.md)). A hardware plan whose own run must start from a
  built-in start says `start = START`; its pass then comes from a gate batch run from that start.
  `fresh` (the card as shipped) is a start on every unit. The names and slots follow the
  example write policy (`examples/write-policy/`); a setup with other rules points `starts` in
  config.toml at its own folder.
- **Every plan runs from the messy starts.** A hardware plan's go needs a run from `dirty` and
  from `occupied` (where the unit has them) besides its own pass. Where release empties the
  test slots (`clear_at_release`, section 6), a unit is never found with them full but always
  as a release leaves it, so `testslot-cleared` takes the place of `occupied`. Each must pass, or stop
  where the plan says it stops (next point). A plan that runs half way and then fails from a
  messy start would do the same on a unit left that way. A plan that flashes is left out: it
  starts on the OS before its flash (often stock, where no start can be built), and the flash is
  gated as a transition.
- **Contrast runs: a check must be seen to fail.** A section names the starts from which it
  must stop the run, and why: `say contrast occupied, testslot: slot 121 holds a project, so
  the cleared-slot check stops`. The run from that start has to stop in that section: not
  earlier (the check was never reached), and not pass (the check cannot tell the two states
  apart). The first section, the start check, names at least one where the unit has built-in
  starts. A readback plan that only passes after another plan names `fresh` (a default project
  has no kept value), and its own pass comes from `run_plan` after that plan.
- **A pass shows what ran before it.** A pass on the session's emulator records the plans and
  calls that ran on it since it booted, and the go request lists them. A pass that ran after an
  ungated write (an emulator-only plan that saves or sends, or a `steps` or `data` write) is
  not recorded: boot a fresh emulator or use a gate batch.

*elkbench enforces*: a gate batch runs each plan from every start its go needs, side by side,
each on a fresh emulator (a built-in start is built once per build and kept as a snapshot where
the emulator has snapshots), and records each run that went as the plan says. `approve_plan`
and `approval_text` refuse a plan with a start run missing, and the go request lists every
start run with its outcome. The plan checker refuses a contrast that names no start the unit
has, a start named in two sections, and, where the unit has built-in starts, a start check
with no contrast; it flags `set-and-kept` and `works` sections that no contrast stops.

## 2. One go per run

- **The owner approves the whole run once**: the unit, the builds with their sha256 and
  emulator results, every flash in order including the stock restore, every write with its
  target (a slot, or "kit 1 of the active project (a test project)") and the settings it will
  change. It never promises that a slot is empty: it says what happens if the slot is occupied. Anything outside that (another build, another unit, a slot
  not named) needs a new go. Each flash is listed on its own, so a file flashed twice in a run
  (stock between two builds and again last) appears twice.
- **Say what each check proves.** For every new feature, the go says whether its steps prove
  only that a setting is made and kept, or that the feature works (its effect measured: a data
  read, a value moving on screen, a recording, or the ear where the feature makes sound), and
  how. A feature shown working only in the emulator, or only set and kept on the unit, is listed
  as a gap. A listening check proves only what is audible.
- **Ask first, claim after the yes.** Nobody is blocked while the owner decides, and the go
  lasts 2 hours. After the yes, claim the unit (queueing if need be) and check its state before
  writing: the handoff note, the OS, the slots the run will write. If the state no longer
  matches what the go covered, or isn't the state the plans were gated on, release the unit with
  nothing written and ask again. Anything said about a unit's state comes from a fresh read of
  the claim tool's status, with its time, not from an earlier read or a handoff note.
- **Only the owner's own answer is a yes.** A note relayed by another session, agent or bot
  never counts, however it is worded.
- Record the unit's state at the start (OS version, a screenshot, the project list) and compare
  at the end.

- **Ask with the exact text.** When the go is asked in a chat thread, post elkbench's own request
  text (`approval_text`), so one typed yes answers both the thread and elkbench, and nothing is
  found wrong with the go only after the yes.
- **Plan from the latest state.** Take the unit's OS, active project and slots from the latest
  handoff note and listing (`unit_state`, with their age), not from older notes, and check them
  again on the unit after claiming.

*elkbench enforces*: hardware plans, flashes and Total Recall restores need an approval token.
A token is bound to the session, covers each item once, and expires after 2 h. `approve_plan`
refuses while the session holds a unit it isn't using. Every hardware call needs the session's
own claim (elklock).

## 3. During a run

- **No key stays held.** Every key session ends with a cancel, and a failed run still lets go
  of keys and the claim.
- **Audio at a safe level.** Never touch the main volume, keep test signals at about -24 dBFS,
  and assume the unit is connected to speakers.
- **No power cycling from software**, and no factory or service console commands. They bypass
  the OS's own safety.
- Change USB CONFIG only when the test needs it, and put it back. A switch drops Overbridge and
  the audio devices for anything else running.
- **Every wait on the unit has a timeout** (flash, reboot, a long transfer). Report a stuck
  unit; don't retry it blindly.
- **Stop at the first unexpected state**: a screen that doesn't match, a reply that isn't the
  one expected, a unit that doesn't answer. Take a screenshot, let go of keys, record the
  unit's state, then report. Don't improvise a recovery. A power cycle or the unit's start-up
  menu is the owner's call.
- **A stalled flash ends the run**: it shows PREPARING but never erases the flash, there's no
  reply, or it times out. Don't flash again as a recovery attempt, not even stock. The owner
  decides what happens next.

*elkbench enforces*: OS upgrades run under the claim, so it can't end mid-flash, and elkflash
waits for the restart with a timeout. The rest of this section is up to the session.

## 4. The claim

- **One writer per unit.** Claim before anything that talks to a unit, read the previous
  holder's handoff note, and never break someone else's claim. The claim covers the unit, not
  what is on it: another session may clear the test slots between your runs (section 6), so
  nothing carries over from one claim to the next (section 1).
- **Going back to the emulator gives the unit back.** If a test has to change mid-run, release
  the unit (with a note naming the OS left on it, even a test build), fix and re-run in the
  emulator, then claim again. Never hold hardware while doing emulator work.
- **Release the moment you're done**, also after a failure, with a note on the state left: the
  OS, the active project, open menus, changed settings.

*elkbench enforces*: an emulator call from a session holding a unit releases it first, unless
the session's own hardware run is still going or someone is listening. A claim ends after
15 min with no device use (never during a run or a listening test), and a claim that ends by
itself records the OS and screen in its handoff note. `release` writes the handoff note and a
row in the unit's test log, and asks for the stock OS first.

## 5. Listening tests

- When a test needs a person to listen, **keep the claim for the whole listening test** and
  **send nothing to the unit until they say they have finished**: no keys, MIDI, screenshots,
  flashes or release, and no moving on to the next step. A fixed wait is never a substitute
  for their answer.
- Before asking, put the unit in the state to be heard, with a sound that is actually audible
  (check the level and the MIDI channel), and say exactly what to listen for and what, if
  anything, the person may do on the unit by hand, including for a plugin pause whether edits in
  the plugin are allowed (write both in the `listen` text). The
  instructions don't change during the listening test: a change is a change to the test
  (section 4, going back to the emulator).

*elkbench enforces*: `listen` (or a `listen TEXT` step in a plan, which pauses the run) holds
the unit. The claim can't time out, every call to that unit is refused, the elklock hook denies
device commands, and release waits. Only the person ends it: `listen_done` with their reply, or
their answer to the client's prompt. A session can't end it itself.

## 6. The owner's own work

- **Never write over what isn't the test's.** Decide on a way to recognise test items (a name
  prefix, reserved project slots) and only write, rename, clear or delete items that carry it,
  or slots that are empty. Everything else is read-only.
- Kits and sounds cross projects. A kit or sound dump received lands in the *active* project,
  and the +Drive sound library and samples are shared. So make a test project active before
  sending any dump, and mark anything saved to the +Drive.
- **Write to the active project only once it is known to be a test project**, in the same run.
  Build the test project in a known state from CREATE NEW, on the Rytm MKII and the Analog
  Four MKII alike: LOAD PROJECT's last row, CREATE NEW, makes a new working project (answer NO to
  "save before loading" and YES to "changes will be lost"); SAVE PROJECT AS then puts it into the
  test slot and makes the saved copy active; loading the test slot proves that, because LOAD asks
  only PROCEED for the active project's own slot. Load it from MANAGE PROJECTS (LOAD FROM), which
  lists every slot: the LOAD PROJECT list shows only filled slots, so its positions change with
  what the unit holds. A new project saved into another slot without
  loading it leaves the previous project active, and a write meant for the test project lands in
  that one instead. SAVE PROJECT AS carries the working project's state, so it starts a test only
  right after CREATE NEW, never from someone's project. Then confirm on screen that the new
  project is the one loaded (its home screen). If the run can't confirm which project is
  active, it writes nothing to it.
- **On the Analog Four MKII, INIT NEW is not new in a used slot.** INIT NEW into a slot that has
  ever held a project brings back that project's kit, sounds and pattern data (only the name and
  tempo are new), however the slot was cleared: data-store clear, DELETE, or the panel's CLEAR
  (which leaves a project named CLEAN). CREATE NEW starts from nothing, which is one reason the
  test project comes from it.
- **Loading another project drops unsaved changes** in the working one. Before the first load,
  know that the working project is saved or is a test project, or have the owner's go cover the
  load. Never save their project yourself.
- **Global settings**: don't save them. If a test needs a changed setting, change the live
  setting and put it back before release; list it in the handoff note.
- **Leave the unit on its stock OS** at the end, and say so in the handoff note. If the restore
  can't run, release with a note that the unit is not on stock and what it is on, ask for the
  restore at once, and nobody else takes the unit until stock is back or the owner clears it.
- Test items stay unless the plan says to clean up; the handoff note lists them.

**An example: the rules elkbench was developed with.** They come with it as a ready write
policy, `examples/write-policy/policy.py` (tests beside it).
- **A name prefix:** everything a test creates or changes is named `~T ` followed by a name
  (`~T PROJ 1`, `~T KIT A`). A session may write, overwrite, rename or clear only an item whose
  name starts with `~T`, or an empty slot (an empty project slot only in 121-128), and it names
  an empty slot `~T ...` in the same step.
  `~` is accepted everywhere a name goes (the unit's name editor, Transfer's rename, SysEx and
  the data store), it should sort after letters and digits (not checked), and nobody names their own work with a
  leading tilde.
- **Reserved project slots:** 121-128 are for tests; the owner keeps 1-120. Outside them, only
  items already named `~T` may be touched. Any session may empty a test slot, whichever run
  filled it, after copying it to its run folder; so no plan relies on what another run left
  there.
- **Test slots empty at every release:** every run ends with 121-128 empty, whatever it put
  there and however it ended: `clear_at_release = '121-128'` in each unit's section of
  config.toml (Rytm, A4, Digitone) makes `release` back each project there up into the run folder, erase it and list
  the slots empty, and keeps the claim if it can't. So a run finds them empty, and its setup's
  start check (121 empty) only stops it when a session ended without releasing. Nobody saves
  their own work there by hand, so the cleanup touches only test projects.
- **The active project:** the unit doesn't say which project is active, so by convention a test
  project names its kit 1 `~T ...`, and the policy checks the active kit 1 before any kit or
  sound dump is sent. On the Digitone and Digitakt the marker is pattern A01's name instead
  (`~T ...`), read back with a stored-pattern dump request, and there is no key-driven kit
  save to check. The policy never names the marker itself: after a save it can't tell which
  project is active, so a plan names its test project's marker on the unit.
- Names: at most 15 characters (the unit stores 16), upper case, from A-Z, 0-9, space and
  `+ - = & / # @ ? % $ ~`. The unit drops any other character silently.
- A save into a test slot that already holds a project empties it first, keeping a copy in the
  run folder: SAVE PROJECT AS asks an extra question when the slot holds another project.

*elkbench enforces*: writes on a unit go through analoghealth's runner, which asks a **local
write policy** before every write, rename, clear, dump and key-driven save; a refusal stops the
step before anything is sent. The policy is a Python file on your computer (the hooks are in
analoghealth's `tools/writepolicy.py`): use the example as it is, change its prefix and slots,
or write your own. With `require_write_policy = true` in `config.toml`, hardware tools stay off
while no policy is installed.

## 7. Shared things

- **The Overbridge Engine is shared** by every unit in Overbridge mode. Anything that drives it
  (Total Recall, a plugin or a DAW run by a session), or quits or restarts Overbridge, holds a
  claim on every unit, connected or not, since a unit switched on midway joins the same Engine.
  Ask the person before quitting or restarting Overbridge.
- Never drive Transfer's or Overbridge's own windows unattended; those steps are the person's.
  Close Transfer's use of a port before MIDI work.

*elkbench enforces*: Total Recall needs claims on all three units; the elklock hook does the
same for commands that drive or stop the Engine.

## 8. Remote control on stock firmware

A stock OS has no remote keys, so a test that drives stock behaviour from a computer flashes a
remote-only build instead: the stock OS with only the remote keys (and encoder turns) patched
in. `standalone/patch.py` in [analoghealth](https://github.com/Iced-Cream/analoghealth) builds
one for the Analog Rytm MKII and Analog Four MKII, and the same in
[digihealth](https://github.com/irpina/digihealth) for the Digitone, from your own stock OS
file. There is none for the Digitakt yet.

- It is still a build: the emulator gate, one go per run and the restore to stock all apply.
- **Name it in results** ("stock 1.74 + remote-only build"). A result counts as stock behaviour
  when it is about what the OS does with the input (menus, pages, values, the sequencer, saved
  data, sound) and the same input on the stock image in the emulator agrees.
- **Not for** tests of the input path itself (encoder acceleration, dead zones, key repeat,
  timing finer than a UI tick), SysEx handling, or the firmware file and upgrade path: there the
  patch differs from stock. Such results need a hand check on true stock.
- A remote turn back by the same counts can land off its start, so plans must not assume a turn
  undoes itself.
- **Size a turn for what it drives.** Remote turns are raw encoder counts, and the OS decides
  what they do.
  - A parameter page moves with a few counts.
  - A list menu needs many. Measured on a Rytm MKII with OS 1.74, LFO DESTINATION list, with
    counts sent 8 per UI tick:
    - 16 counts from rest move nothing, 32 reach the 2nd entry;
    - after that it takes about 16-32 counts per entry.
    - The emulator agrees, except that it moves at 16.
    - Sent 1 per tick, the emulator needs 42-56 for the first entry and 24-32 per entry after it.
  - Move a list with one large turn (e.g. `turn D 32 8` for the first entry, `turn D 32 8` for
    each one or two after), not many small ones.
  - Check where it landed with a shot or `expect-screen` rather than counting turns.
- **A turn that seems to do nothing**: before reporting a tool fault, send the same counts as
  panel turns in the emulator (dtboot `--turn ENC:DELTA@ATxCOUNT/SPACING`) and compare. If those
  don't move it either, the counts were too few.

*elkbench enforces*: release asks for stock over a remote-only build like any other. List
remote-only builds under `no_stats` in `config.toml` and plans or steps with a `stats` step are
refused on them, since their channel has no stats command.

## 9. Evidence and reporting

- Every run keeps its evidence (screenshots, recordings, logs, flash logs) in a run folder of
  its own, outside any public repo. Captures that show the owner's own project names or samples
  are never published.
- Every hardware run gets a row in the unit's test log the same day: the date, each build and
  its sha256, what was checked, and the result.
- The handoff note names the active project the unit is left with (its name, and whether it is a
  test project); the next session checks that on the unit before writing to the active project.
- What a session learns about a unit or a tool (a stuck reboot, a timing, an odd behaviour)
  gets written down where the next session will find it.

## 10. Plans: what a check must show

A pass means "these steps passed from some state". These rules make sure that state is the one
the plan meant, and that each check would have failed if the result were wrong.

- **A start check.** A plan's first section proves the state it starts from on screen, before
  anything is written: the right page, the cleared test slot, the value a power cycle had to keep.
  It names a start from which it stops the run (`say contrast START: why`), so it has been seen
  to fail.
- **Prove content, not a name.** A precondition shows something only this run's setup produced
  (on the Rytm, a new project's title row reads `K001:KIT 1` and `BPM:120.0`). A check of a name
  the plan's own save step has just written passes on a copy of any project.
- **Say what each section proves**: `setup` (state for later checks), `set-and-kept` (a setting is
  made and read back), `works` (the feature's effect is measured) or `evidence` (shots for a person
  to judge). A `set-and-kept` or `works` section needs a machine assertion (`expect-screen`,
  `capture ... any|none`, `audio-compare`, `audio-dropouts`, `stats`, `ping`). A shot is judged by
  eye, and the go request says so.
- **Compare rows that can differ.** A reference compares the rows that show the value under test,
  not the rows that show which page is open, and never rows that are blank in the reference.
- **Known navigation.** Every page is entered from a known page, every menu from its top, and a
  MIDI channel gets a readback before anything depends on it.
- **One reference set per plan.** Plans in one run never compare the same reference file:
  recording one plan's references would void the other's pass.
- **Remote keys need a remote-key path in the OS.** A plan with key, turn or read (`dump`) steps
  runs on a build with one, never on stock.
- **Hits that sound.** On the Rytm, notes 0-11 on the auto channel start tracks; other notes play
  nothing, and the emulator makes no voice sound either. A capture runs behind a level check that
  fails on silence (`audio-dropouts` alone passes on silence). Reads of a voice's values go after
  its first hit.
- **A fixed listen format.** A `listen` step says what to hear, `allowed:` what the person may do
  by hand (or nothing), and `needs:` the audible precondition, which the plan proves on screen
  first (the play indicator, say). A different instruction is a different plan, with its own
  emulator pass.
- **NO to "save before loading".** A load can stop on "SAVE 'X' BEFORE LOADING?" when the working
  project is modified. YES there writes the working project over its own slot, which may hold
  someone's work. A plan proves that prompt with an `expect-screen` named `save-before-load...`
  and answers NO; the checker refuses anything else after it. The box is as wide as the project's
  name, so the check compares the prompt's fixed text only, with `rows=` and `cols=`. The prompt
  is skipped when the active project came from the slot being loaded: the load then goes
  straight to PROCEED, which is how a setup plan proves its test project is active after
  CREATE NEW and SAVE PROJECT AS. With no unsaved changes the OS loads without either prompt, so a
  plan that checks the prompts makes a change first (a turn).
- **A flash names what it flashes over.** A flash plan sets `os = FILE`, the OS on the unit
  before the flash: two flashes of the same file over different builds are different
  transitions, each with its own emulator pass (stock back over each build in a run, say).

*elkbench enforces*: `approve_plan` refuses a hardware plan that has no start check, a section
that doesn't say what it proves (`say proves KIND: how`), a `set-and-kept` or `works` section
without a machine assertion, a `listen` step without its three parts or a screen check before it,
key or read steps after a stock flash, anything but NO after a `save-before-load` check, a flash
plan without `os =`, and plans that compare the same reference file. It shows
the rest in the go request: the sections judged by eye, checks right after the plan's own save,
blank compared rows, Rytm notes outside 0-11. `check_plan` gives the same answer in seconds,
before any emulator time. `templates/` holds plans that already follow these rules: a test
project setup, a flash gate, a power-cycle readback, a capture behind a level check, and a
listening check.
