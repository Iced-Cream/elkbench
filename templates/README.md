# Plan templates

Plans that start from these carry the recipes earlier runs found by trial, and pass the plan
checker (`check_plan`) as they are. Copy one beside your plans, keep its structure, and change
only what its comments name. Each template has its own `refs` folder: keep one per plan.

| template | for |
|---|---|
| `setup-test-project.txt` | the run's own test project: CREATE NEW, SAVE PROJECT AS into a cleared test slot, then LOAD FROM it in MANAGE PROJECTS (the LOAD list shows only filled slots, so its positions move), where PROCEED as the only question proves it is the active project, and the whole home screen proves it is new. Afterwards the active project comes from the test slot whatever it was before, so the start check needs no active-project marker |
| `flash-gate.txt` | one flash transition: one copy per flash, in the run's order, stock back over the last build included; `os =` names the OS it flashes over, and its gate boots that file |
| `power-cycle-readback.txt` | a value kept across a power cycle: read back, `power-cycle` (in the emulator a power-off save and a cold boot, on the unit by the person's hand), read back again |
| `capture-with-level-check.txt` | an audio take behind a level check, so a silent take stops the run |
| `listen.txt` | a listening check in the fixed three-part format, its precondition proven on screen first |

The key sequences are for the Analog Rytm MKII on OS 1.74 with a remote-key path in the OS
(analoghealth, or a remote-only build); the Analog Four MKII's menus differ. No reference
screens ship: record each plan's own in the emulator (`run_plan ... record_refs`), and check the
compared rows against what will differ on the unit before asking for a go.

Each template's start check names its contrast (`say contrast START: why`): the built-in
starts (`starts/`) from which it must stop the run. A gate batch runs it from those and from
`dirty` and `occupied`. The capture, listen and readback templates stop on a fresh emulator, so
their own pass comes from `run_plan` after the setup plan (or the plan that set the kept value).

Open, so far:
- **Backup and clear of the test slot** happen outside the plan: at the previous release where
  the unit sets `clear_at_release` (config.toml), else as hand steps (`data get`, `data clear`)
  listed in the go request.
- **The level check's reference take** (`level-ref.wav`) is recorded once on the unit from the
  same steps; the emulator makes no voice sound.
- **A run nonce in names** (`~T {run}`) is not filled in by elkbench: a reference screen showing
  the name would differ on every run, so the templates prove content instead.
