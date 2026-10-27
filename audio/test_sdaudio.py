"""sdaudio.py against a stand-in sounddevice module (no audio device needed):
python -m unittest test_sdaudio (from this folder)."""
import os
import sys
import tempfile
import types
import unittest

import numpy as np

calls = []
fake = types.ModuleType('sounddevice')
fake.query_devices = lambda: [
    {'name': 'HDA Intel PCH: ALC892 Analog (hw:0,0)', 'max_input_channels': 2, 'max_output_channels': 2},
    {'name': 'Analog Rytm MKII: USB Audio (hw:2,0)', 'max_input_channels': 12, 'max_output_channels': 12}]
fake.rec = lambda n, samplerate, channels, dtype, device: (
    calls.append(('rec', device, channels)), np.zeros((n, channels), 'float32'))[1]
fake.playrec = lambda data, samplerate, channels, dtype, device: (
    calls.append(('playrec', device, data.shape)), np.repeat(data[:, :1], channels, 1))[1]
fake.wait = lambda: None
sys.modules['sounddevice'] = fake
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sdaudio      # noqa: E402


class SdAudio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        calls.clear()

    def test_device_by_name(self):
        self.assertEqual(sdaudio.find_device('Elektron Analog Rytm MKII')[0], 1)
        with self.assertRaises(SystemExit):
            sdaudio.find_device('Analog Four')

    def test_rec_and_tone_write_float_wavs(self):
        out = os.path.join(self.tmp.name, 'r.wav')
        self.assertEqual(sdaudio.main_rec(['rec', 'Elektron Analog Rytm MKII', '0.1', out]), 0)
        self.assertEqual(calls[-1], ('rec', 1, 12))
        with open(out, 'rb') as fh:
            head = fh.read(44)
        self.assertEqual((head[:4], head[20:22], head[22]), (b'RIFF', b'\x03\x00', 12))
        self.assertTrue(os.path.exists(out + '.channels.txt'))
        self.assertEqual(sdaudio.main_tone(['Analog Rytm', out, '0.2', '2']), 0)
        kind, device, shape = calls[-1]
        self.assertEqual((kind, device, shape[1]), ('playrec', 1, 12))


if __name__ == '__main__':
    unittest.main()
