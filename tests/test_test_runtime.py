"""Real private socket paths and synthetic cgroup data, never host services."""
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from tools.test_runtime import cgroup_diagnostics, offline_environment, parse_pressure, short_temporary_directory
from tools import offline_test_guard as guard


PSI = 'some avg10=1.25 avg60=0.50 avg300=0.10 total=123\nfull avg10=0.25 avg60=0.10 avg300=0.01 total=45\n'


class RuntimeTests(unittest.TestCase):
    def test_long_inherited_tmpdir_still_allows_real_unix_socket(self):
        with tempfile.TemporaryDirectory() as base:
            long = Path(base)/('long-'*40)
            long.mkdir()
            with patch.dict(os.environ, {'TMPDIR':str(long)}), short_temporary_directory() as short:
                with socket.socket(socket.AF_UNIX) as server:
                    address = str(short/'test.sock')
                    self.assertLess(len(os.fsencode(address)), 108)
                    server.bind(address)
                self.assertEqual(short.parent, Path('/tmp'))
            self.assertFalse(short.exists())
            self.assertTrue(long.is_dir())

    def test_private_environment_cannot_reenable_desktop_or_integration(self):
        with short_temporary_directory() as short:
            private=short/'private'
            with patch.dict(os.environ, {'DISPLAY':':0','WAYLAND_DISPLAY':'wayland-0',
                                         'MULTILINUX_INTEGRATION':'1','HOME':'/foreign'}):
                env=offline_environment(Path('/repo'), private, short/'tmp', {'LC_ALL':'zh_CN.UTF-8'})
            self.assertNotIn('DISPLAY',env)
            self.assertNotIn('WAYLAND_DISPLAY',env)
            self.assertEqual(env['MULTILINUX_INTEGRATION'],'0')
            self.assertEqual(env['HOME'],str(private/'home'))
            self.assertEqual(env['LC_ALL'],'zh_CN.UTF-8')
            self.assertEqual(Path(env['XDG_RUNTIME_DIR']).stat().st_mode & 0o777,0o700)
            for override in ({'TMPDIR':'/foreign'},{'HOME':'/foreign'},
                             {'MULTILINUX_INTEGRATION':'1'},{'DISPLAY':':0'}, {'PATH':3}):
                with self.subTest(override=override),self.assertRaises(ValueError):
                    offline_environment(Path('/repo'),private,short/'tmp',override)

    def test_pressure_keeps_real_totals_and_rejects_missing_or_bad_values(self):
        parsed=parse_pressure(PSI)
        self.assertEqual(parsed['some']['total'],123)
        self.assertEqual(parsed['full']['avg10'],0.25)
        for bad in ('',PSI.splitlines()[0], PSI+PSI.splitlines()[0],
                    PSI.replace('avg10=1.25','avg10=nan'),PSI.replace('total=123','total=-1'),
                    PSI.replace('avg60=0.50 ',''),PSI.replace('avg10=1.25','avg10=101'),
                    PSI.replace('avg10=1.25','avg10=1 avg10=2')):
            with self.subTest(bad=bad),self.assertRaises(ValueError):parse_pressure(bad)

    def test_file_cache_swap_and_anonymous_memory_are_separate(self):
        with short_temporary_directory() as group:
            (group/'memory.pressure').write_text(PSI)
            (group/'memory.swap.current').write_text('4096\n')
            (group/'memory.stat').write_text('anon 40\nfile 258\npgscan 7\nworkingset_refault_file 3\n')
            data=cgroup_diagnostics(group)
            self.assertEqual(data['swap_current'],4096)
            self.assertEqual(data['memory_stat'],{'anon':40,'file':258,'pgscan':7,'workingset_refault_file':3})
            (group/'memory.pressure').unlink()
            with self.assertRaises(FileNotFoundError):cgroup_diagnostics(group)
            (group/'memory.pressure').write_text(PSI)
            (group/'memory.stat').write_text('anon 40\n')
            with self.assertRaises(ValueError):cgroup_diagnostics(group)
            (group/'memory.stat').write_text('x'*65537)
            with self.assertRaises(ValueError):cgroup_diagnostics(group)


class SupervisorTests(unittest.TestCase):
    def test_failed_or_missing_workload_receipt_never_becomes_success(self):
        self.assertEqual(guard.result_exit_code(None,{'exit_code':0}),0)
        self.assertEqual(guard.result_exit_code(None,{'exit_code':7}),1)
        self.assertEqual(guard.result_exit_code('ABORTED_PSI',{'exit_code':0}),2)
        for value in (None,{}, {'exit_code':False}):
            self.assertEqual(guard.result_exit_code(None,value),2)

    def test_config_accepts_only_the_bounded_offline_budget(self):
        config={'unit':'unifier-t1-fixture','memory_mib':512,'reserve_bytes':1024**3,
                'seconds':300,'root':str(guard.ROOT),'command':['/usr/bin/true']}
        for memory in (256, 384, 512, 768, 1024):
            with self.subTest(memory=memory):
                guard.validate_spec({**config,'memory_mib':memory})
        for key,value in (('memory_mib',255),('memory_mib',1025),('memory_mib',True),
                          ('reserve_bytes',0),('seconds',301),
                          ('command',[]),('unit','foreign.service'),('root','/foreign')):
            with self.subTest(key=key),self.assertRaises(ValueError):
                guard.validate_spec({**config,key:value})
        for memory in (256, 1024):
            with self.assertRaisesRegex(ValueError,'changed safety budget'):
                guard.validate_spec({**config,'scope':'host-fuse','memory_mib':memory,
                                     'seconds':90,'unit':'unifier-t2-fuse-fixture'})

    def test_server_build_is_scoped_and_charges_the_full_admission_margin(self):
        config = {'unit': 'unifier-t1-build', 'memory_mib': 2048, 'server_build': True,
                  'reserve_bytes': 1024**3, 'seconds': 300, 'root': str(guard.ROOT),
                  'command': ['/usr/bin/true']}
        with patch.object(guard, 'on_build_server', return_value=True):
            guard.validate_spec(config)
            for changed in ({'memory_mib': 2049}, {'scope': 'host-fuse'},
                            {'server_build': 1}, {'server_build': False}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    guard.validate_spec({**config, **changed})
        with patch.object(guard, 'on_build_server', return_value=False), self.assertRaises(ValueError):
            guard.validate_spec(config)
        for budget, required in ((256, 1472), (512, 1728), (1024, 2240), (2048, 3348)):
            self.assertEqual(guard.admission_bytes(budget, 1024**3), required * guard.MIB)

    def test_replaced_unit_is_not_stopped(self):
        original={'InvocationID':'old','ControlGroup':'/gone'}
        with patch.object(guard,'fields',return_value={'InvocationID':'new','ControlGroup':'/gone'}),patch.object(guard,'call') as stop:
            with self.assertRaisesRegex(RuntimeError,'identity changed'):
                guard.finish_owned_unit(original,{},'owned.service')
            stop.assert_not_called()

    def test_existing_evidence_directory_is_never_overwritten(self):
        with short_temporary_directory() as existing,patch.object(guard.subprocess,'call') as launch:
            (existing/'abort.json').write_text('original')
            with self.assertRaises(FileExistsError):
                guard.main(['run','--out',str(existing),'--','/usr/bin/true'])
            self.assertEqual((existing/'abort.json').read_text(),'original')
            launch.assert_not_called()


if __name__=='__main__':unittest.main()
