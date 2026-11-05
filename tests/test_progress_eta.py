"""ETA is sampled progress metadata, never an installation deadline."""
import contextlib
import io
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_common as common


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ProgressETATests(unittest.TestCase):
    @contextlib.contextmanager
    def terminal(self, now):
        stream = Terminal()
        with mock.patch.object(common, '_progress', None), \
             mock.patch.object(common.time, 'monotonic', side_effect=lambda: now[0]), \
             mock.patch.object(common.threading, 'Thread'), contextlib.redirect_stderr(stream), \
             common.progress_session():
            yield stream

    def test_estimate_counts_down_but_expires_without_fresh_samples(self):
        now = [100.0]
        with self.terminal(now) as stream:
            common.progress('下载', remaining=125)
            self.assertIn('当前环节预计剩余 约02:05', stream.getvalue())
            now[0] = 110
            common._progress._write(waiting=True)
            self.assertIn('约01:55', stream.getvalue().splitlines()[-1])
            now[0] = 130
            common._progress._write(waiting=True)
            self.assertIn('当前环节预计剩余 未知', stream.getvalue().splitlines()[-1])

    def test_elapsed_estimate_is_unknown_until_explicit_completion(self):
        now = [0.0]
        with self.terminal(now) as stream:
            common.progress('下载', remaining=2)
            now[0] = 3
            common._progress._write()
            self.assertIn('预计剩余 未知', stream.getvalue().splitlines()[-1])
            common.progress('下载校验完成', remaining=0)
            self.assertIn('预计剩余 约00:00', stream.getvalue().splitlines()[-1])

    def test_new_stage_clears_estimate_and_long_estimates_show_hours(self):
        now = [0.0]
        with self.terminal(now) as stream:
            common.progress('下载', remaining=3661)
            self.assertIn('约01:01:01', stream.getvalue())
            now[0] = 10
            common.progress('编译')
            line = stream.getvalue().splitlines()[-1]
            self.assertIn('本阶段 00:00', line)
            self.assertIn('预计剩余 未知', line)

    def test_same_sample_refreshes_age_without_duplicate_output(self):
        now = [0.0]
        with self.terminal(now) as stream:
            common.progress('下载', remaining=120)
            now[0] = 25
            common.progress('下载', remaining=120)
            self.assertEqual(len(stream.getvalue().splitlines()), 1)
            now[0] = 35
            common._progress._write()
            self.assertIn('约01:50', stream.getvalue().splitlines()[-1])

    def test_bad_estimates_become_unknown_without_breaking_the_operation(self):
        now = [0.0]
        for estimate in [None, -1, float('nan'), float('inf'), True, '120', 10 ** 1000]:
            with self.subTest(estimate=type(estimate).__name__), self.terminal(now) as stream:
                common.progress('下载', remaining=estimate)
                self.assertIn('预计剩余 未知', stream.getvalue())

    def test_redirected_output_stays_silent_and_enabled_tracks_session(self):
        stream = io.StringIO()
        with mock.patch.object(common, '_progress', None), \
             mock.patch.object(common.threading, 'Thread') as thread, contextlib.redirect_stderr(stream):
            with common.progress_session():
                self.assertFalse(common.progress_enabled())
                common.progress('下载', remaining=60)
            thread.assert_not_called()
        self.assertEqual(stream.getvalue(), '')
        with self.terminal([0.0]):
            self.assertTrue(common.progress_enabled())
            common._progress.stopped.set()
            self.assertFalse(common.progress_enabled())


if __name__ == '__main__':
    unittest.main()
