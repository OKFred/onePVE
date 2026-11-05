import contextlib
from datetime import datetime as real_datetime
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'lib'))
import onepve_sls_budget as budget

ACCOUNT='1234567890123456'


def compete(path,month,gate,answers):
    gate.wait()
    try:budget.reserve(path,ACCOUNT,month,1);answers.put('reserved')
    except budget.CostLimitError:answers.put('stopped')


@unittest.skipIf(os.name=='posix' and os.geteuid()!=0,'PVE root-private budgets require root')
class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'budget'
        self.stack=contextlib.ExitStack();self.addCleanup(self.stack.close)
        if budget.fcntl is None:
            self.stack.enter_context(mock.patch.object(budget,'fcntl',types.SimpleNamespace(flock=lambda *args:None,LOCK_EX=2)))
        self.month=budget._month()

    def initialize(self,**options):
        return budget.init_budget(self.root,ACCOUNT,**options)

    def test_attempts_are_reserved_before_send_and_never_refunded(self):
        self.initialize(raw_limit=10,request_limit=3)
        for count in [1,2]:
            answer=budget.reserve(self.root,ACCOUNT,self.month,5)
            self.assertEqual(answer['sequence'],count)
        self.assertEqual(answer['raw_bytes_remaining'],0)
        with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,self.month,1)
        # Metadata GETs consume a request even with a zero-byte body.
        budget.reserve(self.root,ACCOUNT,self.month,0)
        with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,self.month,0)
        self.assertEqual(budget.status(self.root,ACCOUNT)['requests_used'],3)

    def test_atomic_write_failure_does_not_authorize_a_network_call(self):
        self.initialize();sender=mock.Mock();before=(self.root/'budget.json').read_bytes()
        with mock.patch.object(budget,'atomic_json',side_effect=OSError('disk full')):
            with self.assertRaises(budget.CostLimitError):
                budget.reserve(self.root,ACCOUNT,self.month,10)
                sender()
        sender.assert_not_called();self.assertEqual((self.root/'budget.json').read_bytes(),before)

    def test_repeat_approval_limits_or_credential_identity_cannot_reset_usage(self):
        self.initialize(raw_limit=10,request_limit=2);budget.reserve(self.root,ACCOUNT,self.month,1)
        for action in [lambda:budget.init_budget(self.root,ACCOUNT),
                       lambda:budget.approve_month(self.root,ACCOUNT),
                       lambda:budget.reserve(self.root,'9876543210987654',self.month,1),
                       lambda:budget.init_budget(self.root,'9876543210987654'),
                       lambda:budget.reserve(self.root,ACCOUNT,self.month,1,raw_limit=11),
                       lambda:budget.reserve(self.root,ACCOUNT,self.month,1,request_limit=3)]:
            with self.assertRaises(budget.CostLimitError):action()
        self.assertEqual(budget.status(self.root,ACCOUNT)['raw_bytes_used'],1)
        budget.reserve(self.root,ACCOUNT,self.month,1)  # No credential material is part of this ledger.
        self.assertEqual(budget.status(self.root,ACCOUNT)['requests_used'],2)

    def test_month_changes_require_explicit_approval_and_keep_old_evidence(self):
        class Clock:
            value=real_datetime.fromisoformat('2026-11-30T15:54:00+00:00')
            @classmethod
            def now(cls,tz):return cls.value.astimezone(tz)
        with mock.patch.object(budget,'datetime',Clock):
            self.assertEqual(budget._month(),'2026-11')
            self.initialize();budget.reserve(self.root,ACCOUNT,'2026-11',100)
            Clock.value=real_datetime.fromisoformat('2026-11-30T16:00:00+00:00')
            self.assertEqual(budget._month(),'2026-12');self.assertFalse(budget.status(self.root,ACCOUNT)['approved'])
            with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,'2026-12',1)
            with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,'2026-11',1)
            with self.assertRaises(budget.CostLimitError):budget.approve_month(self.root,ACCOUNT,'2027-01')
            budget.approve_month(self.root,ACCOUNT,'2026-12',raw_limit=2,request_limit=1)
            budget.reserve(self.root,ACCOUNT,'2026-12',2)
            data=json.loads((self.root/'budget.json').read_text())
            self.assertEqual(data['months']['2026-11']['raw_bytes_used'],100)
            self.assertEqual(data['months']['2026-12']['raw_bytes_used'],2)

    def test_missing_corrupt_or_foreign_files_fail_closed(self):
        self.initialize();path=self.root/'budget.json';saved=path.read_bytes()
        changed=json.loads(saved);changed['months'][self.month]['raw_limit']-=1
        for value in [b'{',b'{}',saved.replace(b'1234567890123456',b'9876543210987654'),json.dumps(changed).encode()]:
            path.write_bytes(value)
            with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,self.month,0)
            with self.assertRaises(budget.CostLimitError):budget.status(self.root,ACCOUNT)
        path.unlink()
        with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,self.month,0)
        with self.assertRaises(budget.CostLimitError):self.initialize()
        path.write_bytes(saved)
        (self.root/'identity.json').unlink()
        with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,self.month,0)

    def test_waiting_for_lock_across_midnight_cannot_spend_last_month(self):
        class Clock:
            value=real_datetime.fromisoformat('2026-11-30T15:54:00+00:00')
            @classmethod
            def now(cls,tz):return cls.value.astimezone(tz)
        original=budget._lock
        @contextlib.contextmanager
        def waited(directory):
            with original(directory):
                Clock.value=real_datetime.fromisoformat('2026-11-30T16:00:00+00:00')
                yield
        with mock.patch.object(budget,'datetime',Clock):
            self.initialize()
            with mock.patch.object(budget,'_lock',waited),self.assertRaises(budget.CostLimitError):
                budget.reserve(self.root,ACCOUNT,'2026-11',1)
        data=json.loads((self.root/'budget.json').read_text())
        self.assertEqual(data['months']['2026-11']['requests_used'],0)

    def test_month_end_safety_window_pauses_attempts_without_charging_budget(self):
        class Clock:
            value=real_datetime.fromisoformat('2026-11-30T15:54:00+00:00')
            @classmethod
            def now(cls,tz):return cls.value.astimezone(tz)
        with mock.patch.object(budget,'datetime',Clock):
            self.initialize();budget.reserve(self.root,ACCOUNT,'2026-11',1)
            Clock.value=real_datetime.fromisoformat('2026-11-30T15:56:00+00:00')
            before=(self.root/'budget.json').read_bytes()
            with self.assertRaisesRegex(budget.CostLimitError,'Month-end safety window'):
                budget.reserve(self.root,ACCOUNT,'2026-11',1)
            self.assertEqual(before,(self.root/'budget.json').read_bytes())
            self.assertEqual(budget.status(self.root,ACCOUNT)['requests_used'],1)
            approved=budget.init_budget(Path(self.temp.name)/'late-approval',ACCOUNT)
            self.assertTrue(approved['approved'])

    def test_hard_ceilings_and_attempt_types_are_enforced(self):
        for options in [dict(raw_limit=budget.MAX_RAW+1),dict(request_limit=budget.MAX_REQUESTS+1),
                        dict(raw_limit=True),dict(request_limit=0)]:
            with self.assertRaises(budget.CostLimitError):self.initialize(**options)
        self.initialize()
        for raw,count in [(-1,1),(True,1),(1,False),(0,0),(0,1.0),(budget.MAX_RAW+1,1)]:
            with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,self.month,raw,count)
        self.assertEqual(budget.status(self.root,ACCOUNT)['requests_used'],0)

    def test_status_is_read_only_and_does_not_create_missing_resources(self):
        with self.assertRaises(budget.CostLimitError):budget.status(self.root,ACCOUNT)
        self.assertFalse(self.root.exists())
        self.initialize();before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.root.iterdir()}
        budget.status(self.root,ACCOUNT)
        self.assertEqual(before,{p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in self.root.iterdir()})
        self.assertEqual(budget.ROOT,Path('/var/lib/onepve/sls/budget'))

    @unittest.skipUnless(os.name=='posix','POSIX ownership/permissions and links')
    def test_permissions_and_links_are_never_adopted(self):
        self.initialize();path=self.root/'budget.json';path.chmod(0o644)
        with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,self.month,0)
        path.chmod(0o600);path.rename(self.root/'saved.json');path.symlink_to(self.root/'saved.json')
        with self.assertRaises(budget.CostLimitError):budget.reserve(self.root,ACCOUNT,self.month,0)

    @unittest.skipUnless(os.name=='posix','Real Linux process/file locking')
    def test_two_processes_cannot_spend_the_same_last_request(self):
        self.initialize(raw_limit=1,request_limit=1)
        ctx=multiprocessing.get_context('fork');gate=ctx.Event();answers=ctx.Queue()
        children=[ctx.Process(target=compete,args=(self.root,self.month,gate,answers)) for _ in range(2)]
        for child in children:child.start()
        gate.set()
        try:
            values=[answers.get(timeout=5) for _ in children]
            for child in children:child.join(timeout=5);self.assertEqual(child.exitcode,0)
            self.assertCountEqual(values,['reserved','stopped'])
        finally:
            for child in children:
                if child.is_alive():child.terminate();child.join(timeout=5)
            answers.close();answers.join_thread()
        self.assertEqual(budget.status(self.root,ACCOUNT)['requests_used'],1)


if __name__=='__main__':unittest.main()
