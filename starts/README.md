# Built-in emulator starts

Emulator-only plans that leave a fresh emulator the way a unit is usually found. A gate batch
runs each hardware plan from them too (`dirty` and `occupied` always, where the unit has them,
and every start a plan's `say contrast START: why` lines name), and the go needs each of those
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
it. Its runs from `dirty` and `occupied` and from its contrasts' starts are as for any plan.

What the emulator's card holds (analog-emu, seen 2026-11-02): two projects in slots 001 and 002,
002 active after boot, its pattern A01 on kit 1, slots 121-128 empty. It boots with two prompts.

Known limits:
- A freshly booted project already counts as modified (a LOAD asks to save it before any
  change), so the unsaved change is shown by the SRC page shots before and after the turn,
  not by a check.
- The start plans end in shots, not reference checks: no reference screens ship. Each start's
  build run keeps its shots with its snapshot (`run_dir` in the snapshot's `.json`).
- There are no starts for the Analog Four MKII, Digitone or Digitakt yet: those units are gated
  from `fresh` and from the starts a plan's contrasts name (none but `fresh` there).
