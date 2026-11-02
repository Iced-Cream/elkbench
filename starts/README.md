# Built-in emulator starts

Emulator-only plans that leave a fresh emulator the way a unit is usually found. A gate batch
runs each hardware plan from them too (`dirty` and `occupied` always, where the unit has them;
`testslot-cleared` instead of `occupied` where the unit sets `clear_at_release`, as every
release then leaves the unit that way and never with its test slots full; and every start a plan's `say contrast START: why` lines name), and the go needs each of those
runs to pass or to stop where the plan's contrast says. See
[docs/testing-practices.md](../docs/testing-practices.md), "Messy starts and contrast runs".

`fresh` (a fresh emulator, the card as shipped) is a start on every unit and has no file.
A start is the file `<unit>/<start>.txt`; config.toml's `starts` points elkbench at another
folder. These follow the example write policy (`examples/write-policy/`): `~T ` test names and
test slots 121-128. A setup with other rules keeps its own copies.

## Analog Rytm MKII (OS 1.74 with a remote-key path)

| start | leaves |
|---|---|
| `dirty` | the card's own project (slot 002) active, pattern A02, which brings kit 2 (another kit and BPM in the title row), an unsaved change on BD's SRC page, the LFO page open |
| `testslot` | a new test project `~T START` in slot 121, loaded and active, an unsaved change on BD's SRC page, the LFO page open |
| `occupied` | slots 121-128 holding new test projects `~T OCC1`-`~T OCC8`, the card's own project still active |
| `testslot-cleared` | `testslot`, then slot 121 cleared through the data store, as a run's backup and clear leaves a unit: `~T START` still the working project, 121 empty with the active-project marker on it |

## Analog Four MKII (OS 1.56 with analoghealth)

| start | leaves |
|---|---|
| `dirty` | the card's own project (slot 002) active, pattern A02, which brings kit 2 (another kit and BPM in the title row), an unsaved change on T1's OSC1 LEV, the LFO page open |
| `testslot` | a new test project `~T START` (CREATE NEW, then SAVE PROJECT AS into 121), active, an unsaved change on T1's OSC1 LEV, the LFO page open |
| `occupied` | slots 121-128 holding `~T OCC1`-`~T OCC8` (SAVE PROJECT AS copies of the card's project), the card's own project loaded back and active |
| `testslot-cleared` | `testslot`, then slot 121 cleared through the data store, as a run's backup and clear leaves a unit: `~T START` still the working project, 121 empty with the active-project marker on it |

## Digitone (OS 1.44 with a remote-key path, such as a digihealth build)

| start | leaves |
|---|---|
| `dirty` | the card's own project (slot 001) loaded as booted, an unsaved change from knob A, track 1's TRIG page open; no pattern change (see below) |
| `testslot` | a new test project `~T START` (CREATE NEW, then SAVE PROJECT AS into 121), active, an unsaved change from knob A, track 1's TRIG page open |
| `occupied` | slots 121-128 holding `~T OCC1`-`~T OCC8` (SAVE PROJECT AS copies of the card's project); the last copy, in 128, is the active project |
| `testslot-cleared` | `testslot`, then slot 121 cleared through the data store, as every release with `clear_at_release` leaves a unit: `~T START` still the working project, 121 empty with the active-project marker on it |

Every Digitone hardware plan starts from empty test slots: the Digitone templates say
`start = testslot-cleared`, so a gate batch runs each one's own run from that start, and the
`occupied` and `testslot` runs (121 holds a project) must stop at the empty-slot check. digiemu
keeps no start snapshots yet, so each run from a start builds it first (`testslot-cleared`
builds `testslot`, then clears 121): a Digitone gate takes longer than a Rytm one.

The A4 builds its test projects from CREATE NEW, not INIT NEW: INIT NEW into a slot that once held
a project brings back that project's kit 1 (see `templates/a4/setup-test-project.txt`). SAVE
PROJECT AS makes the saved copy active, so `occupied` loads the card's project back last.

## Building on a start, and data steps

A start file may take two settings besides a plan's own:

- `base = START` builds on another start of the same unit: that start runs first, on the same
  emulator (a chain of bases is fine; a loop is refused).
- `data = OP PATH [FILE]; ...` runs data store steps on the emulator after the base and before
  the start's own sections: `clear PATH` empties a slot, `put PATH FILE` writes an object saved
  with the data tool's `get` (FILE relative to the start file). They go through analogrun's
  `data`, so the local write policy is asked, as for the data tool. They reach states the
  front panel cannot build, such as a project loaded from a test slot that was then cleared.
  A local starts folder can use `put` to load its own `~T` backups into the test slots, so the
  emulator holds what the unit holds; keep such files out of shared repositories.

A start still needs one section of its own (an evidence shot is enough). Its snapshot key covers
the text of every start in its chain and the files its data steps put.

A hardware plan whose own run should start from a built-in start, not from a fresh emulator,
says so with `start = START` (a plan for a state only a start leaves, such as a setup plan
for a unit whose active project came from 121 before 121 was cleared: `start = testslot-cleared`). Its pass then comes
from a gate batch, which builds that start and runs the plan from it; run_plan does not record
it. Its runs from the required starts and from its contrasts' starts are as for any plan: a
required start that the plan's start check rightly rejects is named in that check's contrast
(`say contrast dirty, occupied, testslot: 121 is not the cleared active slot`), so its run must
stop there rather than pass.

What the emulator's card holds (analog-emu, seen 2026-11-02):
- Analog Rytm MKII: two projects in slots 001 and 002, 002 active after boot, its pattern A01 on
  kit 1, slots 121-128 empty. It boots with two prompts.
- Analog Four MKII: two projects in slots 001 (write-protected) and 002, both with the same name,
  002 active after boot, its pattern A01 on kit 1 (80 BPM; A02 brings kit 2 at 98 BPM), slots
  121-128 empty. It boots to the home screen with no prompt; the starts' NOs at boot do nothing
  there.

- Digitone: one project in slot 001 (write-protected, a lock mark in its row), no slot holds the
  active-project marker until a SAVE PROJECT AS, pattern A01 on its own sound (86.0 BPM; A02, picked
  with PTN and a trig key, has another sound at 92.0 BPM), slots 002-128 empty. It boots to the
  home screen with no prompt; the starts' NOs do nothing there. SAVE PROJECT AS makes the saved
  copy active, and one to seven `DOWN` after the 15 `FUNC(DOWN)` in its slot list reach 122-128. CREATE NEW
  keeps the selected pattern, so the Digitone `dirty` start leaves pattern A01.

Known limits:
- On the Rytm a freshly booted project already counts as modified (a LOAD asks to save it before
  any change), so the unsaved change is shown by the SRC page shots before and after the turn,
  not by a check. On the A4 a project with no change loads without a question, so its starts
  turn T1's LEVEL before every load, and the unsaved change is shown by the OSC1 page shots (LEV
  shows its new value for a moment after the turn).
- The start plans end in shots, not reference checks: no reference screens ship. Each start's
  build run keeps its shots with its snapshot (`run_dir` in the snapshot's `.json`).
- There are no starts for the Digitakt yet: it is gated from `fresh` only.
