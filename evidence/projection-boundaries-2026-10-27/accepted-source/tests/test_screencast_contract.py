"""Check the independently written C and Python probes' metadata boundary."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.probe_screencast import _consumer_errors, _summary_metrics, _result_metadata


class NativeMetadata(unittest.TestCase):
    def test_native_hash_samples_and_top_colors_are_recognized(self):
        summary = {'passed': True, 'mode': 'memfd', 'buffer_type': 'MemFd',
                   'frames': 60, 'gpu_import': False, 'cpu_readback': True,
                   'hashes_first': ['1234'], 'hashes_last': ['5678'],
                   'top_colors': [{'rgb': '#b12b31', 'count': 100}]}
        child = {'returncode': 0, 'summary': summary, 'events': []}
        self.assertEqual(_consumer_errors('memfd', 60, child), [])
        hashes, colors = _summary_metrics(summary, [])
        self.assertEqual(hashes, ['1234', '5678'])
        self.assertEqual(colors[0]['count'], 100)

    def test_absent_native_pixel_evidence_cannot_pass(self):
        summary = {'passed': True, 'mode': 'dmabuf', 'buffer_type': 'DmaBuf',
                   'frames': 60, 'gpu_import': True, 'cpu_readback': True,
                   'hashes_first': [], 'hashes_last': [], 'top_colors': []}
        errors = _consumer_errors('dmabuf', 60, {
            'returncode': 0, 'summary': summary, 'events': []})
        self.assertIn('consumer summary has no pixel hashes', errors)
        self.assertIn('consumer summary has no color counts', errors)

    def test_native_format_event_supplies_dimensions(self):
        result = _result_metadata(SimpleNamespace(mode='memfd'), 'memfd',
                                  {'frames': 60, 'format': 'BGRx'},
                                  [{'type': 'format', 'width': 900, 'height': 598}], 0)
        self.assertEqual((result['width'], result['height']), (900, 598))
        self.assertFalse(result['zero_copy'])


if __name__ == '__main__':
    unittest.main()
