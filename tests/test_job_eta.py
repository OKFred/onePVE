"""APT telemetry is bounded, fresh and never changes job lifecycle decisions."""
import ast
import base64
import contextlib
import io
import os
from pathlib import Path
import shlex
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import onepve_agent as agent
import onepve_common as common
import test_progress as progress_tests
from test_progress import Terminal
from test_agent import state


class JobEtaTests(unittest.TestCase):
    def test_apt_native_remaining_supports_documented_and_trixie_units(self):
        for token,seconds in [('4m40s',280),('4min 40s',280),('1h 2min 3s',3723),('1d 1h 1min 1s',90061),('2s',2)]:
            with self.subTest(token=token):
                detail,remaining=agent._job_log_progress('dlstatus:1:9.05654:Retrieving file 1 of 3 ('+token+' remaining)\n')
                self.assertEqual(remaining,seconds)
                self.assertEqual(detail,'APT 软件包下载 9.1%')
        self.assertEqual(agent._job_log_progress('dlstatus:1:25:Retrieving file 1 of 3')[1],None)

    def test_malformed_or_completed_progress_never_reuses_an_estimate(self):
        start='dlstatus:1:20:Retrieving file 1 of 3 (4min 40s remaining)\n'
        for tail in ['pmstatus:hello:15:Configuring hello', 'dlstatus:1:100:Done',
                     'dlstatus:1:nan:Bad', 'dlstatus:1:inf:Bad', 'dlstatus:1:200:Bad',
                     'dlstatus:broken','dlstatus:1:20:Bad (9999999999999s remaining)',
                     '[onePVE guest] /usr/sbin/runuser -u agent -- fnm exec node --version',
                     '[onePVE phase] driver-build', 'Building module(s)....',
                     '[onePVE phase] driver-verify']:
            with self.subTest(tail=tail):
                detail,remaining=agent._job_log_progress(start+tail)
                self.assertIsNone(remaining)
                self.assertNotIn('runuser',detail)
        self.assertIn('解包／配置 15.0%',agent._job_log_progress('pmstatus:hello:15:Configuring hello')[0])
        self.assertIsNone(agent._job_log_progress('old task log without telemetry')[1])

    def test_log_tail_is_bounded_and_closes_handle_even_on_error(self):
        q=mock.Mock()
        q.call.side_effect=[7,{'position':20000},{'position':3616},
                            {'buf-b64':base64.b64encode(b'partial\ndlstatus:1:20:Downloading (2s remaining)\n').decode()},{}]
        size,text=agent._job_log_tail(q,{'path':'/jobs/test.json'})
        self.assertEqual(size,20000)
        self.assertTrue(text.startswith(b'dlstatus:'))
        self.assertIn(mock.call('guest-file-seek',{'handle':7,'offset':3616,'whence':'set'}),q.call.call_args_list)
        self.assertIn(mock.call('guest-file-read',{'handle':7,'count':16384}),q.call.call_args_list)
        self.assertEqual(q.call.call_args_list[-1],mock.call('guest-file-close',{'handle':7}))
        q.call.side_effect=[7,RuntimeError('read unavailable'),{}]
        with self.assertRaisesRegex(RuntimeError,'unavailable'):
            agent._job_log_tail(q,{'path':'/jobs/test.json'})
        self.assertEqual(q.call.call_args_list[-1],mock.call('guest-file-close',{'handle':7}))

    def job_samples(self, samples, tty=True):
        now=[0.0]; q=progress_tests.ProgressTests.guest(); output=Terminal() if tty else io.StringIO()
        samples=[(item[0],item[1].encode()) if isinstance(item,tuple) else item for item in samples]
        results=iter([b'{"status":"RUNNING"}']*len(samples)+[b'{"status":"SUCCESS"}'])
        q.read.side_effect=lambda path:next(results)
        def sleep(seconds):now[0]+=10
        with mock.patch.object(common,'_progress',None),contextlib.redirect_stderr(output), \
             mock.patch.object(common.threading,'Thread'),common.progress_session(), \
             mock.patch.object(agent,'Qga',return_value=q),mock.patch.object(agent,'_save'), \
             mock.patch.object(agent,'_job_execution',return_value={'stopped':False,'boot_id':'same-boot'}), \
             mock.patch.object(agent.time,'monotonic',side_effect=lambda:now[0]), \
             mock.patch.object(agent.time,'sleep',side_effect=sleep), \
             mock.patch.object(agent,'_job_log_tail',side_effect=samples) as tail:
            answer=agent._job(progress_tests.ProgressTests.job_state(),'install',['COMMAND_SECRET'])
        self.assertEqual(answer['status'],'SUCCESS')
        q.write.assert_not_called()
        self.assertEqual(q.execute.call_count,1)
        self.assertNotIn('COMMAND_SECRET',output.getvalue())
        return output.getvalue(),tail.call_count

    def test_resume_requires_new_log_bytes_before_showing_eta_and_clears_stale(self):
        row='dlstatus:1:25:Retrieving (2min 10s remaining)\n'
        text,calls=self.job_samples([(100,row),(100,row),(200,row),(200,row),
                                     (300,'[onePVE phase] driver-build\n')])
        lines=[x for x in text.splitlines() if '【进度】' in x]
        self.assertEqual(calls,5)
        self.assertEqual(sum('当前环节预计剩余 约02:10' in x for x in lines),1)
        self.assertIn('当前环节预计剩余 未知',lines[-1])
        self.assertIn('编译',lines[-1])

    def test_unrelated_log_growth_cannot_refresh_an_old_download_countdown(self):
        row='dlstatus:1:25:Retrieving (2min 10s remaining)\n'
        rows=[row,row*2,row*2+'more output\n',row*2+'more output\nFetched 10 MB\n']
        text,calls=self.job_samples([(len(log),log) for log in rows])
        lines=[line for line in text.splitlines() if '【进度】' in line]
        self.assertEqual(calls,4)
        self.assertEqual(sum('预计剩余 约02:10' in line for line in lines),1)
        self.assertIn('预计剩余 未知',lines[-1])
        self.assertIn('后续配置',lines[-1])

    def test_optional_sampling_failure_and_nonterminal_do_not_change_success(self):
        text,calls=self.job_samples([RuntimeError('telemetry unavailable')])
        self.assertIn('进度采样暂不可用',text)
        text,calls=self.job_samples([(100,'dlstatus:1:50:Downloading (2s remaining)')],tty=False)
        self.assertEqual(calls,0)
        self.assertEqual(text,'')

    def test_apt_instrumentation_preserves_environment_and_nonapt_commands(self):
        root=Path(__file__).resolve().parents[1]
        driver_state=state(driver_spec=agent.gpu.driver_spec('6.12.113+deb13-amd64'))
        for source in [(root/'guest/install.py').read_text(),agent._driver_script(driver_state)]:
            tree=ast.parse(source)
            function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='run')
            process=mock.Mock()
            scope={'subprocess':process,'os':os,'shlex':shlex,'environment':{'DEBIAN_FRONTEND':'noninteractive','KEEP':'1'}}
            exec(compile(ast.Module(body=[function],type_ignores=[]),'<run>','exec'),scope)
            run=scope['run']
            with contextlib.redirect_stdout(io.StringIO()),mock.patch.dict(os.environ,{'KEEP':'1'}):
                if function.args.vararg:run('apt-get','install','-y','hello')
                else:run(['apt-get','install','-y','hello'],env={'KEEP':'1','DEBIAN_FRONTEND':'noninteractive'})
                call=process.run.call_args
                self.assertEqual(call.args[0],['apt-get','-o','APT::Status-Fd=1','install','-y','hello'])
                self.assertEqual(call.kwargs['env']['LC_ALL'],'C')
                self.assertEqual(call.kwargs['env']['KEEP'],'1')
                self.assertTrue(call.kwargs['check'])
                if not function.args.vararg:
                    run(['apt-get','update'],env={})
                    self.assertEqual(process.run.call_args.kwargs['env'],{'LC_ALL':'C'})
                if function.args.vararg:run('modinfo','i915')
                else:run(['modinfo','i915'])
                self.assertEqual(process.run.call_args.args[0],['modinfo','i915'])
                self.assertNotIn('LC_ALL',process.run.call_args.kwargs.get('env',{}))


if __name__=='__main__':unittest.main()
