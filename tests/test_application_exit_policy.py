"""Foreground lifetime is explicit, pinned and rejected before runtime effects."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unifier import cli
from unifier.errors import UnifierError
from unifier.handlers import execution_fingerprint
from test_on_demand import ready


class ExitPolicy(unittest.TestCase):
    def test_registration_and_cli_require_explicit_on_demand_opt_in(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = ready(Path(temporary))
            self.assertNotIn('exit_policy', service.show_app('notes'))
            args = cli.build_parser().parse_args(['app', 'add', 'debian13', 'writer',
                '--exec', '/usr/bin/true', '--on-demand', '--exit-policy', 'stop-children'])
            with patch('unifier.cli.service_factory', return_value=service), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.cmd_app_add(args), 0)
            self.assertEqual(service.show_app('writer')['exit_policy'], 'stop-children')
            for policy, managed in [('stop-children', False), ('unknown', True), (None, True)]:
                runtime.events.clear()
                with self.assertRaisesRegex(UnifierError, 'exit policy'):
                    service.add_app('debian13', 'invalid', '/usr/bin/true', ['/usr/bin/true'],
                                    on_demand=managed, exit_policy=policy)
                self.assertEqual(runtime.events, [])

    def test_policy_change_invalidates_handlers_but_legacy_wait_keeps_its_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = ready(Path(temporary))
            launch = service.show_app('notes')
            old = execution_fingerprint(launch)
            self.assertEqual(old, execution_fingerprint({**launch, 'exit_policy': 'wait'}))
            launch['exit_policy'] = 'stop-children'
            self.assertNotEqual(old, execution_fingerprint(launch))
            service.store.save_launch(launch)
            runtime.events.clear()
            with self.assertRaisesRegex(UnifierError, 'configuration changed'):
                service.start_app_execution('notes', launch['argv'], token='b'*32,
                                            expected_execution_fingerprint=old)
            self.assertEqual(runtime.events, [])

    def test_invalid_persisted_policy_is_refused_before_container_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = ready(Path(temporary))
            launch = service.show_app('notes'); launch['exit_policy'] = 'unknown'
            service.store.save_launch(launch)
            runtime.events.clear()
            with self.assertRaisesRegex(UnifierError, 'exit policy'):
                service.validate_app_binding('notes')
            with self.assertRaisesRegex(UnifierError, 'exit policy'):
                service.exec_app('notes', launch['argv'])
            self.assertEqual(runtime.events, [])
