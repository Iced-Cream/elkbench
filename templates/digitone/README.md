# Digitone plan templates

The Rytm templates (one folder up), redone for the Digitone on OS 1.44 with a remote-key path in
the OS (a digihealth build). Every key path was found by trial in digiemu. Each template has its
own `refs` folder (`refs/digitone-...`): keep one per plan. No reference screens ship: record each
plan's own in the emulator (`run_plan ... record_refs`).

| template | for |
|---|---|
| `setup-test-project.txt` | the run's own test project: INIT NEW into a cleared slot 121, LOAD FROM it, and prove the load by content (a new project's title row reads A01 UNTITLED and 120.0) |
| `flash-gate.txt` | one flash transition: one copy per flash, in the run's order, stock back over the last build included |
| `capture-with-level-check.txt` | an audio take behind a level check, so a silent take stops the run |
| `listen.txt` | a listening check in the fixed three-part format, behind a level check |

Where the Digitone differs from the Rytm:
- **No play state on screen.** No page tried on OS 1.44 differs between playing and stopped, so a
  level check (on the unit; the emulator plays track 1 too, at its own level) is what proves the
  pattern plays. The listen template has one before its listen step, and needs a `level-ref.wav`.
- **The load prompts come only after an unsaved change.** With another project active, LOAD FROM
  asks SAVE '<name>' BEFORE LOADING? (answered NO) and then PROCEED WITH LOADING?, but only when
  the working project has unsaved changes. The setup template turns an encoder first so both
  always come; YES at PROCEED discards every unsaved change, as on the Rytm.
- **A test project is marked by its pattern A01's name** in a write policy, not by a kit.
- **No `ping` or `capture`** with a digirun that lacks them (digihealth's runner-capture-ping
  adds them), so the flash gate keeps a shot only.
- **No power-cycle template**: the power-cycle step is Rytm and A4 only.

The Digitakt has no templates yet: no digihealth build for it has remote keys, and every template
but the flash gate is key-driven.
