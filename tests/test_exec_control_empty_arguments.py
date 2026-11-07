"""The default APP state-root argument survives the real FD 3 exec helper."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest

from unifier.exec_control import control_exec_argv
from unifier.runtime import application_guest_argv, exec_argv, host_exec_command, CONTROL_FD
from unifier.errors import UnifierError


class EmptyControlArguments(unittest.TestCase):
    def test_default_application_root_is_preserved_as_a_literal_empty_argument(self):
        guest=application_guest_argv('run','a'*32,CONTROL_FD,'/bin/true')
        command=exec_argv('fixed-cid',guest,workdir=None,env={},control_fd=CONTROL_FD)
        wrapped=host_exec_command(4,command)
        self.assertIn('',guest)
        self.assertEqual(wrapped[4:],command)

    def test_actual_helper_exec_preserves_empty_argument_and_trailing_argument(self):
        read_fd,write_fd=os.pipe()
        try:
            argv=control_exec_argv(write_fd,[sys.executable,'-c',
                'import sys;print(repr(sys.argv[1:]))','','after-empty'])
            child=subprocess.Popen(argv,pass_fds=(write_fd,),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            os.close(write_fd);write_fd=None
            stdout,stderr=child.communicate(timeout=5)
            self.assertEqual(child.returncode,0,stderr)
            self.assertEqual(ast.literal_eval(stdout.decode()),['','after-empty'])
        finally:
            os.close(read_fd)
            if write_fd is not None:os.close(write_fd)

    def test_missing_program_nonstrings_and_nul_refuse_before_execution(self):
        for command in ([],[''],[None],['/bin/true',None],['/bin/true','\0']):
            with self.subTest(command=command),self.assertRaises(UnifierError) as failure:
                host_exec_command(4,command)
            self.assertIs(failure.exception.execution_started,False)


if __name__=='__main__':unittest.main()
