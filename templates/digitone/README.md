# Digitone plan templates

The Rytm templates (one folder up), redone for the Digitone on OS 1.44 with a remote-key path in
the OS (a digihealth build). Every key path was found by trial in digiemu. Each template has its
own `refs` folder (`refs/digitone-...`): keep one per plan. No reference screens ship: record each
plan's own in the emulator (`run_plan ... record_refs`).

| template | for |
|---|---|
| `setup-test-project.txt` | the run's own test project: CREATE NEW, SAVE PROJECT AS 121, LOAD FROM 121 (PROCEED only proves 121 is active), and prove it new by content (the title row reads A01 UNTITLED and 120.0). Not INIT NEW: into the active project's slot after a data clear, it keeps records of the old project. Passes with 121 cleared while still active, or with another project active |
| `flash-gate.txt` | one flash transition: one copy per flash, in the run's order, stock back over the last build included |
| `capture-with-level-check.txt` | an audio take behind a level check, so a silent take stops the run |
| `listen.txt` | a listening check in the fixed three-part format, behind a level check |
| `power-cycle-readback.txt` | a value kept across a power cycle (the tempo, set by keys and read on the home screen): read back, `power-cycle`, read back again |

Setup, capture and listen start from empty test slots: each says `start = testslot-cleared`, so a
gate batch runs its own run from that built-in start (the state every release with
`clear_at_release` leaves), and its start check names `occupied` and `testslot` in its contrast
(121 holds a project there, so the empty-slot check stops the run). See `starts/README.md`.

Where the Digitone differs from the Rytm:
- **Screen checks compare whole rows.** digirun's `expect-screen` takes `rows=` and does not honour
  `cols=` (it ignores it), so every Digitone check names rows only, and a check stays clear of rows
  whose content varies from unit to unit: the active-project marker in the project lists, a box as
  wide as a project's name, a list that shows as many rows as the unit holds projects.
- **No play state on screen.** No page tried on OS 1.44 differs between playing and stopped, so a
  level check (on the unit; the emulator plays track 1 too, at its own level) is what proves the
  pattern plays. The listen template has one before its listen step, and needs a `level-ref.wav`.
- **The load prompts come only after an unsaved change.** With another project active, LOAD FROM
  asks SAVE '<name>' BEFORE LOADING? (answered NO) and then PROCEED WITH LOADING?, but only when
  the working project has unsaved changes (CREATE NEW asks the same). The setup template turns an
  encoder first so they always come; YES at PROCEED discards every unsaved change, as on the Rytm.
- **LOAD FROM the active project's own slot asks only PROCEED.** The setup uses that as its proof
  that 121 is the active project after SAVE PROJECT AS. The start check leaves out the active
  marker's rows, so a slot cleared while active passes as empty.
- **A test project is marked by its pattern A01's name** in a write policy, not by a kit.
- **Keys after a load need digirun to reopen its key session.** The load stops the unit
  answering; a digirun from digihealth branch `digirun-reopen-capture-ping` (or
  `digirun-reopen-session`) opens a new key session for the next key (an older one stops the plan
  there).
- **Capture and listen carry the setup inside**, with `snapshot-after` on its LOAD FROM section:
  in a digiemu with setup snapshots the emulator runs the setup once per build and resumes it,
  and the T1 sections run after each resume.
- **`ping` and `capture` need a digirun that has them.** digihealth branch
  `digirun-reopen-capture-ping` has both and the key-session reopen; the flash gate's `ping`
  answers on stock 1.44 too, as it uses Elektron's own RPC. An older digirun refuses both steps.
- **The power cycle runs in digiemu** with a digiemu whose usbrun has `--power-off`: the OS's own
  power-off save, then a boot on that card (about a minute; the cold boot, intro and settle).
  After it the template waits 40 s, as an OS with remote keys answers them that long after it
  starts on the unit. A run with it starts from empty test slots, like every Digitone hardware run.

The Digitakt has no templates yet: no digihealth build for it has remote keys, and every template
but the flash gate is key-driven.
