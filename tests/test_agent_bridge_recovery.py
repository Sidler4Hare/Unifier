"""Offline crash-window and cancellation regressions for request recovery."""
import contextlib
import io
import json
import os
import threading
import time
from unittest.mock import patch
from test_agent_bridge import BridgeHarness
from unifier import cli
from unifier.errors import UnifierError


class BridgeRecovery(BridgeHarness):
    def request(self):
        return json.loads(self.request_text(action_id='run-echo'))

    def make_dead_pending(self, bridge, request_uuid, *, state='unresolved'):
        with bridge.ledger.collection.locked(True):
            items = bridge.ledger._read()
            item = next(x for x in items if x['request_uuid'] == request_uuid)
            item.update(state=state, owner={'pid': 99999999, 'stamp': 1, 'boot_id': 'previous-boot'})
            bridge.ledger.collection.write_unlocked(items)
        return item

    def test_binding_write_failure_prevents_guest_submission(self):
        bridge = self.build()
        with patch.object(bridge.ledger, 'bind_execution', side_effect=OSError('disk full')), \
             patch.object(self.sessions, 'execute') as execute:
            result = bridge.respond(self.request())
        self.assertEqual(result['status'], 'failed')
        execute.assert_not_called()

    def test_cli_recovers_dead_request_without_opening_listener(self):
        bridge = self.build()
        request = self.request()
        self.assertEqual(bridge.respond(request)['status'], 'ok')
        pending = self.make_dead_pending(bridge, request['request_uuid'])
        session_uuid = pending['execution']['session_uuid']
        completed = self.sessions.store.read(session_uuid)
        self.sessions.store.update(session_uuid, worker_stamp=completed['worker_stamp'] + 1)
        policy = self.policy_file()
        output = io.StringIO()
        with (patch.dict(os.environ, {'UNIFIER_HOME': str(self.root)}),
              patch('unifier.agent_bridge.service_for_root', return_value=self.sessions.service) as factory,
              patch('unifier.agent_bridge.AgentBridge.bind') as bind,
              patch('unifier.project_sessions.ProjectSessions.execute') as execute,
              contextlib.redirect_stdout(output)):
            code = cli.main(['agent', 'serve', '--policy', str(policy), '--recover'])
        self.assertEqual(code, 0)
        factory.assert_called_once_with(str(self.root))
        bind.assert_not_called()
        execute.assert_not_called()
        rows = json.loads(output.getvalue())
        self.assertEqual([row['action'] for row in rows], ['recovered'])
        self.assertFalse(rows[0]['replayed'])
        self.assertEqual(bridge.respond(request)['status'], 'rejected')

    def test_dead_completed_session_reconciles_without_replay_and_remains_deduplicated(self):
        bridge = self.build()
        request = self.request()
        result = bridge.respond(request)
        self.assertEqual(result['status'], 'ok')
        row = self.make_dead_pending(bridge, request['request_uuid'])
        self.assertEqual(row['execution']['session_uuid'], result['receipt']['session']['session_uuid'])
        session_uuid = row['execution']['session_uuid']
        completed = self.sessions.store.read(session_uuid)
        # execute() ran in this test process. Simulate the original worker
        # having exited and its PID being reused, as well as the dead broker.
        self.sessions.store.update(
            session_uuid, worker_stamp=completed['worker_stamp'] + 1)
        view = self.sessions.show(session_uuid)
        self.assertFalse(view['worker_live'])
        self.assertFalse(view['child_live'])
        self.assertEqual(view['guest_state'], 'gone')
        self.assertFalse(view['lease_live'])
        self.assertIsNone(view['lease_token'])
        with patch.object(self.sessions, 'execute') as execute:
            recovered = bridge.recover_requests()
            self.assertEqual(recovered[0]['action'], 'recovered')
            self.assertEqual(bridge.recover_requests(), [])
        execute.assert_not_called()
        self.assertEqual(bridge.respond(request)['status'], 'rejected')
        saved = bridge.ledger.records()[0]
        self.assertEqual(saved['state'], 'failed')
        self.assertFalse(saved['recovery']['replayed'])
        self.assertIn('receipt', saved)

    def test_dead_broker_with_live_session_keeps_fence_without_recovery_or_replay(self):
        bridge = self.build()
        request = self.request()
        self.assertEqual(bridge.respond(request)['status'], 'ok')
        row = self.make_dead_pending(bridge, request['request_uuid'])
        self.assertTrue(self.sessions.show(row['execution']['session_uuid'])['worker_live'])
        with patch.object(self.sessions, 'execute') as execute, \
             patch.object(self.sessions, 'recover') as recover:
            result = bridge.recover_requests()
            self.assertEqual(result[0]['action'], 'kept-live-session')
            self.assertEqual(bridge.respond(request)['status'], 'rejected')
        execute.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(bridge.ledger.records()[0], row)
        self.assertTrue(bridge._degraded())

    def test_live_broker_and_legacy_claim_do_not_clear_fence(self):
        bridge = self.build()
        request = self.request()
        bridge.ledger.reserve(request['request_uuid'], 'run-echo', self.origin, ttl_seconds=60)
        self.assertEqual(bridge.recover_requests()[0]['action'], 'kept-live-broker')
        self.make_dead_pending(bridge, request['request_uuid'], state='claimed')
        self.assertEqual(bridge.recover_requests()[0]['action'], 'unresolved')
        self.assertTrue(bridge._degraded())

    def test_changed_session_binding_is_not_recovered(self):
        bridge = self.build()
        request = self.request()
        bridge.respond(request)
        self.make_dead_pending(bridge, request['request_uuid'])
        original = self.sessions.show
        def changed(sid):
            view = original(sid)
            view['argv'] = ['unexpected']
            return view
        with patch.object(self.sessions, 'show', side_effect=changed):
            self.assertEqual(bridge.recover_requests()[0]['action'], 'unresolved')
        self.assertTrue(bridge._degraded())

    def test_stop_flag_interrupts_long_join_and_cancels_owned_session(self):
        bridge = self.build(document=self.policy_document(actions=[self.run_action(timeout_seconds=30)]))
        self.runtime.hold = True
        request = self.request()
        answer = {}
        thread = threading.Thread(target=lambda: answer.update(bridge.respond(request)))
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = bridge.ledger.records()
            if rows and rows[0].get('execution') and self.runtime.last_execution:
                break
            time.sleep(.01)
        bridge._stopping = True
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertNotEqual(answer.get('status'), 'ok')
