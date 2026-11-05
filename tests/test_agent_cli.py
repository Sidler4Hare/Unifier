"""The public bridge entry keeps help offline and credentials out of argv."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unifier import cli


class AgentCLI(unittest.TestCase):
    def test_help_does_not_resolve_an_environment_or_touch_installation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "absent"
            with patch.dict(os.environ, {"UNIFIER_HOME": str(root)}), \
                    patch.object(cli, "service_factory", side_effect=AssertionError("runtime")), \
                    redirect_stdout(StringIO()) as out, self.assertRaises(SystemExit) as caught:
                cli.main(["agent", "serve", "--help"])
            self.assertEqual(caught.exception.code, 0)
            self.assertIn("--policy", out.getvalue())
            self.assertFalse(root.exists())

    def test_serve_uses_explicit_installation_without_exposing_a_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"UNIFIER_HOME": temporary}), \
                    patch("unifier.agent_bridge.main", return_value=2) as serve:
                code = cli.main(["agent", "serve", "--policy", "/private/grant.json", "--once"])
            self.assertEqual(code, 2)
            argv = serve.call_args.args[0]
            self.assertEqual(argv[argv.index("--root") + 1], temporary)
            self.assertIn("--once", argv)

    def test_request_uses_an_environment_reference_and_preserves_refusal(self):
        secret = "a" * 64
        with patch.dict(os.environ, {"TEST_BRIDGE_TOKEN": secret}), \
                patch("unifier.bridge_client.main", return_value=2) as request:
            code = cli.main(["agent", "request", "--socket", "/private/bridge.sock",
                             "--action", "host-info", "--token-env", "TEST_BRIDGE_TOKEN"])
        self.assertEqual(code, 2)
        self.assertNotIn(secret, repr(request.call_args))
        self.assertIn("TEST_BRIDGE_TOKEN", request.call_args.args[0])

    def test_missing_token_refuses_before_connect_without_echoing_values(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("unifier.bridge_client.request", side_effect=AssertionError("connected")), \
                redirect_stderr(StringIO()) as error:
            code = cli.main(["agent", "request", "--socket", "/private/bridge.sock",
                             "--action", "host-info", "--token-env", "TEST_BRIDGE_TOKEN"])
        self.assertEqual(code, 1)
        self.assertIn("missing-token", error.getvalue())


if __name__ == "__main__":
    unittest.main()
