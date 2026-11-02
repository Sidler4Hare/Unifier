"""The GUI timing endpoint must use the titled window's real buffer commit."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.probe_startup import WaylandTrace, stats


class StartupTraceTests(unittest.TestCase):
    def test_empty_initial_commit_and_cursor_do_not_count_as_the_window_frame(self):
        trace = WaylandTrace('MLSTARTUP-test')
        lines = [
            'xdg_wm_base#1.get_xdg_surface(new id xdg_surface#2, wl_surface#3)',
            'xdg_surface#2.get_toplevel(new id xdg_toplevel#4)',
            'xdg_toplevel#4.set_title("MLSTARTUP-test.txt - Mousepad")',
            'wl_surface#3.commit()',
            'wl_surface#99.attach(wl_buffer#100, 0, 0)',
            'wl_surface#99.commit()',
            'wl_surface#3.attach(nil, 0, 0)',
            'wl_surface#3.commit()',
        ]
        for index, line in enumerate(lines):
            trace.feed(line, index + 1)
        self.assertIsNone(trace.commit_ns)
        trace.feed('wl_surface#3.attach(wl_buffer#5, 0, 0)', 10)
        trace.feed('wl_surface#3.frame(new id wl_callback#6)', 11)
        trace.feed('wl_surface#3.commit()', 12)
        trace.feed('wl_callback#999.done(1)', 13)
        self.assertIsNone(trace.frame_callback_ns)
        trace.feed('wl_callback#6.done(2)', 14)
        self.assertEqual((trace.attach_ns, trace.commit_ns, trace.frame_callback_ns),
                         (10, 12, 14))

    def test_other_toplevel_and_later_commits_cannot_replace_the_first_endpoint(self):
        trace = WaylandTrace('MLSTARTUP-test')
        lines = [
            'xdg_wm_base@1.get_xdg_surface(new id xdg_surface@2, wl_surface@3)',
            'xdg_surface@2.get_toplevel(new id xdg_toplevel@4)',
            'xdg_toplevel@4.set_title("other window")',
            'wl_surface@3.attach(wl_buffer@5, 0, 0)',
            'wl_surface@3.commit()',
            'xdg_wm_base@1.get_xdg_surface(new id xdg_surface@7, wl_surface@8)',
            'xdg_surface@7.get_toplevel(new id xdg_toplevel@9)',
            'xdg_toplevel@9.set_title("MLSTARTUP-test")',
            'wl_surface@8.attach(wl_buffer@10, 0, 0)',
            'wl_surface@8.commit()',
            'wl_surface@8.attach(wl_buffer@11, 0, 0)',
            'wl_surface@8.commit()',
        ]
        for index, line in enumerate(lines):
            trace.feed(line, index + 1)
        self.assertEqual(trace.surface, '8')
        self.assertEqual((trace.attach_ns, trace.commit_ns), (9, 10))

    def test_small_sample_percentile_is_nearest_rank_and_keeps_outliers(self):
        result = stats([1, 2, 3, 4, 100])
        self.assertEqual(result['p50'], 3)
        self.assertEqual(result['p95'], 100)
        self.assertEqual(result['count'], 5)
        self.assertIsNone(stats([]))


if __name__ == '__main__':
    unittest.main()
