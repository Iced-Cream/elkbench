# Emulator timing and the CPU gate

The emulators normally count instructions, not the unit's cycles, so their CPU readings sit far under the
unit's. analog-emu's unit timing (`DTEMU_TIMING`, branch `unit-timing`) costs the core's cycles and the
memory's waits. On the Rytm, the unit's readings are all taken behind the Sidecar Pi in `usb=mirror`, and the
Pi runs the Overbridge stream both ways all the time (about 6,857 blocks of 368 bytes a second each way).
core 0.10/0.11's memcpy/memset wrappers run for the stream's handlers too. Without the stream the emulator
reads core 0.11 builds about 14 points low; with it, nine of eleven calibration builds land within about
3.6 points of the unit (analog-emu `docs/TIMING.md`, "With the Sidecar's Overbridge stream").

## The option

| where | how |
|---|---|
| `run_plan` / `emulator` call | `timing` = `off`, `unit` or `exact`; `stream`, `cpu_gate` (true/false), `cpu_warn`, `cpu_fail` (CPU %) |
| plan settings | `timing = unit`, `stream = no`, `cpu-gate = no` |
| `config.toml` | an `[emulator]` table: `timing`, `stream`, `cpu_gate`, `cpu_warn` (90), `cpu_fail` (95), `cpu_settle` (90 s), `cpu_readings` (4), `cpu_gap` (5 s), `cpu_quiet` (10 s), `stream_at` (9 s) |

The call wins over the plan, and the plan wins over the config. With none of them naming `timing`, an emulator
starts as it always did. `DTEMU_TIMING` then comes from the daemon's own environment, so existing plans
behave the same.

- **unit / exact.** The emulator starts with `DTEMU_TIMING` set. `unit` is the sampled mode and runs at
  about real time. `exact` costs every block and runs at about 0.6x real time; elkbench waits three times
  as long for its OS to answer.
- **off.** The emulator starts without `DTEMU_TIMING`, even when the daemon's environment has it.
- **stream.** On the Rytm, `stream` defaults on with timing. The emulator then starts with
  `--usb-overbridge` (analog-emu branch `ob-stream-flag`).
  - At `stream_at` seconds of emulated time, the emulator sets USB CONFIG to OVERBRIDGE the way the menu
    does on OS 1.74.
  - The simulated host then leaves for 0.2 s, enumerates again and streams both ways. No keys are pressed.
  - elkbench waits for the switch, at a start and after a power cycle's cold boot, before it sends
    anything. With `--usb-midi-real`, replies go through the simulated host, so a request made while the
    host was away got no answer.
  - The switch waits until the OS has set its USB mode up (for up to 30 s), and it switches again after an
    OS upgrade's reboot. Gap: a step sent within about 0.2 s of that switch after a flash can still
    meet the host away.
  - `stream = no` turns it off. Other units have no stream and refuse `stream = yes`.
- **Refusals.** elkbench refuses a timing that the configured analog-emu cannot run: one with no
  `DTEMU_TIMING` in the binary, or no `--usb-overbridge` in its `--help` while the stream is on.
- **Running emulator.** A `run_plan` without `build` runs on the session's emulator, so that emulator
  must have started with the same timing and stream. If it did not, the run is refused.
- **Setup snapshots.** Runs with a timing on take no setup snapshot and resume none. The setup runs under
  the run's own conditions.
- **Batches.** There is no timing option for batches yet: `batch` refuses `timing`, and config's
  `[emulator]` does not apply to it. Run timing plans one at a time with `run_plan`.

## The CPU gate

The gate runs on every emulator `run_plan` with `unit` or `exact` timing, unless `cpu_gate` is off. It
starts once the plan has passed.

1. **The plan's own readings.** It takes the analoghealth `stats` readings the plan's run log holds, from
   second `cpu_settle` on. The second counted is the firmware's uptime, not wall time.
2. **Its own readings.** It sends nothing for `cpu_quiet` s (10). The run's last steps and elkbench's
   saved-project check after it load the CPU: in C11N's end-to-end run, the first `stats` after the
   project listing read 100 %. It then reads `stats` itself on the emulator, as the plan left it, until
   it has `cpu_readings` more, `cpu_gap` s apart. If the boot has not settled yet, it waits for that
   first.
3. **Verdict.** The highest CPU reading decides:

   | highest CPU | result |
   |---|---|
   | `cpu_fail` or more | the run FAILS |
   | above `cpu_warn` | passes with a "margin risk" line |
   | `cpu_warn` or less | passes |

   - With the defaults, 90.0 passes, 90.1 and 94.9 are risks, and 95.0 fails.
   - No reading at all fails the run ("not read"). A build without analoghealth cannot be gated, so run it
     with `cpu_gate = no`.
   - A plan that leaves the emulator on a stock or `no_stats` OS file is not gated, and the result says so.
4. **Report.** The result lists the timing and every reading as `sSECOND CPU/DSP`. A failed gate records no
   emulator pass.

The readings are idle readings on the emulator's card project, with whatever the plan left on screen. They
match the unit's Sidecar conditions for USB traffic, not the unit's project. Checked end to end on a scratch
daemon (2026-11-12, `timing = unit`, stream on, CTL1 and C11N from `core-ar2-0.11\build`): see the
branch's commit message for the readings (CTL1 80.9-81.6 %, gate PASS; C11N 89.9-90.5 %, PASS with a margin risk).
