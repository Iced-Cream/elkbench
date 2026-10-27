# audio: USB audio from the units

Small Windows tools for recording a unit's USB audio and judging it. elkbench's `audio_record`
and `audio_check` run `asio_rec.py`, `asio_tone.py` and `audiocheck.py` from here; the rest are
for working by hand.

| script | what | needs |
|---|---|---|
| `asio_rec.py` | ASIO host in ctypes: list a driver's channels, or record every input to a multichannel float WAV (channel names in `<out>.channels.txt`). Overbridge mode gives per-track outputs | stdlib |
| `asio_tone.py` | play a steady sine on two outputs while recording every input, for `audiocheck.py dropouts` | stdlib |
| `asio_loop.py` | play a test signal out and back over ASIO: where it returns, its level, the round-trip delay in samples | stdlib |
| `wavein_rec.py` | record a Windows capture device (any USB mode, main stereo) through winmm | stdlib |
| `loopback.py` | the same loopback test through winmm devices | stdlib |
| `audiocheck.py` | pass/fail checks on those recordings: `dropouts` (gaps, clicks) and `compare` (A/B spectrum against a reference) | numpy, scipy |
| `test_audiocheck.py` | synthetic tests for `audiocheck.py`: `python -m unittest test_audiocheck` here | numpy, scipy |
| `analyse.py` | summary (peak, RMS, DC, clipping, L/R correlation, spectrum) and a waveform + spectrogram PNG of a stereo WAV | numpy, Pillow |
| `analyse_multi.py` | the same per channel for a multichannel ASIO recording | numpy, Pillow |

Anything that plays to or records from a unit talks to it: claim the unit with elklock first
(`elklock.py run <unit> -- python asio_rec.py ...`); the hook refuses these without a claim.
