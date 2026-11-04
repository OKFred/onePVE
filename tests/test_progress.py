"""Progress is terminal-only metadata; it must not change JSON or job behavior."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_agent as agent
import onepve_common as common
import onepve_gpu as gpu


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ProgressTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(common, '_progress', None)
        patch.start()
        self.addCleanup(patch.stop)

    @contextlib.contextmanager
    def terminal(self, stream=None):
        stream = Terminal() if stream is None else stream
        with mock.patch.object(common.threading, 'Thread') as thread, \
             contextlib.redirect_stderr(stream), common.progress_session():
            yield stream, thread.return_value

    @staticmethod
    def job_state():
        return {'vmid': 103, 'owner': 'a' * 32, 'phase': 'INSTALL_RUNNING',
                'guest_stage': '/opt/onepve-stage/test'}

    @staticmethod
    def guest():
        guest = mock.MagicMock()
        guest.__enter__.return_value = guest
        guest.execute.return_value = {'returncode': 0, 'stdout': 'LoadState=loaded', 'stderr': ''}
        return guest

    def test_redirected_output_is_silent_and_creates_no_thread(self):
        stderr = io.StringIO()
        with mock.patch.object(common.threading, 'Thread') as thread, contextlib.redirect_stderr(stderr):
            with common.progress_session():
                common.progress('安装桌面', '客机日志：/guest/install.log')
            thread.assert_not_called()
        self.assertEqual(stderr.getvalue(), '')
        self.assertIsNone(common._progress)

    def test_missing_or_invalid_terminal_does_not_block_the_operation(self):
        for failure in [None, OSError('terminal unavailable'), ValueError('stream closed')]:
            with self.subTest(failure=type(failure).__name__):
                stderr = None if failure is None else mock.Mock()
                if stderr is not None:
                    stderr.isatty.side_effect = failure
                completed = []
                with mock.patch.object(common.threading, 'Thread') as thread, contextlib.redirect_stderr(stderr):
                    with common.progress_session():
                        common.progress('安装桌面')
                        completed.append(True)
                self.assertEqual(completed, [True])
                thread.assert_not_called()
                self.assertIsNone(common._progress)
                if stderr is not None:
                    stderr.write.assert_not_called()

    def test_terminal_deduplicates_and_heartbeats_until_exception_cleanup(self):
        now = [100.0]
        with mock.patch.object(common.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(RuntimeError, 'install failed'):
                with self.terminal() as (stderr, thread):
                    common.progress('安装桌面', '客机日志：/guest/install.log')
                    common.progress('安装桌面', '客机日志：/guest/install.log')
                    reporter = common._progress
                    now[0] = 165.0
                    with mock.patch.object(reporter.stopped, 'wait', side_effect=[False, True]) as wait:
                        reporter._heartbeat()
                    self.assertEqual(wait.call_args_list, [mock.call(30), mock.call(30)])
                    raise RuntimeError('install failed')
            self.assertEqual(stderr.getvalue().count('【进度】'), 2)
            self.assertIn('本次已用时 01:05', stderr.getvalue())
            self.assertIn('本阶段 01:05', stderr.getvalue())
            self.assertEqual(stderr.getvalue().count('等待中'), 1)
            self.assertTrue(reporter.stopped.is_set())
            thread.join.assert_called_once_with(timeout=1)
            before = stderr.getvalue()
            reporter._write(waiting=True)
            self.assertEqual(stderr.getvalue(), before)
        self.assertIsNone(common._progress)

    def test_cli_progress_leaves_stdout_as_one_json_document(self):
        document = {'status': 'READY', 'vmid': 103}
        for module, action in [(agent, 'status'), (gpu, 'inventory')]:
            with self.subTest(module=module.__name__):
                def operation(*args):
                    common.progress('读取工作机状态')
                    return document
                stdout = io.StringIO()
                with self.terminal() as (stderr, _), contextlib.redirect_stdout(stdout), \
                     mock.patch.object(module, action, side_effect=operation):
                    self.assertEqual(module.main([action]), 0)
                self.assertEqual(json.loads(stdout.getvalue()), document)
                self.assertNotIn('【进度】', stdout.getvalue())
                self.assertIn('【进度】', stderr.getvalue())

    def test_phase_metadata_does_not_expose_state_or_claim_failed_save(self):
        state = self.job_state()
        state.update(config={'ssh_key': 'KEY_SENTINEL', 'token': 'TOKEN_SENTINEL'},
                     command=['program', '--password', 'COMMAND_SENTINEL'])
        with self.terminal() as (stderr, _), mock.patch.object(agent, 'atomic_json'):
            agent._save(state, 'INSTALL_RUNNING')
            agent._save(state, 'INSTALL_RUNNING')
        text = stderr.getvalue()
        self.assertEqual(text.count('【进度】'), 1)
        self.assertIn('安装桌面、浏览器和 Node.js', text)
        self.assertIn('state.json', text)
        self.assertNotIn('SENTINEL', text)
        with self.terminal() as (stderr, _), \
             mock.patch.object(agent, 'atomic_json', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                agent._save(state, 'READY_GPU')
        self.assertEqual(stderr.getvalue(), '')

    def test_job_reports_derived_log_without_duplicate_launch_or_command_leak(self):
        state, guest = self.job_state(), self.guest()
        guest.read.return_value = b'{"status":"SUCCESS","returncode":0}'
        with self.terminal() as (stderr, _), mock.patch.object(agent, 'Qga', return_value=guest), \
             mock.patch.object(agent, '_save'):
            agent._job(state, 'install', ['/bin/true', 'COMMAND_SECRET_SENTINEL'])
            agent._job(state, 'install', ['/bin/true', 'COMMAND_SECRET_SENTINEL'])
        log = str(Path(state['jobs']['install']['path']).with_suffix('.log'))
        self.assertIn('客机日志：' + log, stderr.getvalue())
        self.assertNotIn('COMMAND_SECRET_SENTINEL', stderr.getvalue())
        self.assertEqual(guest.execute.call_count, 1)
        guest.write.assert_not_called()

    def test_failed_job_still_requires_explicit_retry(self):
        state, guest = self.job_state(), self.guest()
        guest.read.return_value = b'{"status":"FAILED","log":"/guest/failure.log"}'
        with self.terminal(), mock.patch.object(agent, 'Qga', return_value=guest), \
             mock.patch.object(agent, '_save'):
            with self.assertRaisesRegex(RuntimeError, '/guest/failure.log'):
                agent._job(state, 'driver', ['/bin/false'])
            with self.assertRaisesRegex(RuntimeError, 'explicit resume'):
                agent._job(state, 'driver', ['/bin/false'])
        self.assertEqual(guest.execute.call_count, 1)
        guest.write.assert_not_called()

    def test_job_timeout_does_not_restart_uncertain_work(self):
        state, guest, now = self.job_state(), self.guest(), [0.0]
        def still_running(path):
            now[0] = 20.0
            return b'{"status":"RUNNING"}'
        guest.read.side_effect = still_running
        with self.terminal(), mock.patch.object(agent, 'Qga', return_value=guest), \
             mock.patch.object(agent, '_save'), mock.patch.object(agent.time, 'sleep'), \
             mock.patch.object(common.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(TimeoutError, 'resume will observe'):
                agent._job(state, 'install', ['/bin/true'], timeout=10)
        self.assertEqual(guest.execute.call_count, 1)
        guest.write.assert_not_called()

    def test_broken_progress_stream_does_not_interrupt_saved_phase(self):
        class BrokenTerminal(Terminal):
            def write(self, text):
                raise OSError('terminal disconnected')
        with self.terminal(BrokenTerminal()), mock.patch.object(agent, 'atomic_json') as save:
            agent._save(self.job_state(), 'INSTALL_RUNNING')
            self.assertTrue(common._progress.stopped.is_set())
            save.assert_called_once()


if __name__ == '__main__':
    unittest.main()
