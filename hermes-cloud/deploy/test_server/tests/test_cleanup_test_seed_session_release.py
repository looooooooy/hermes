from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from uuid import UUID, uuid5

ROOT = Path(__file__).parents[1]
RUNNER = ROOT / "scripts" / "cleanup_test_seed_session.py"

spec = importlib.util.spec_from_file_location(
    "hermes_cloud_seed_cleanup_release", RUNNER
)
if spec is None or spec.loader is None:
    raise RuntimeError("cleanup runner cannot be loaded")
cleanup_runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cleanup_runner
spec.loader.exec_module(cleanup_runner)


class CleanupTestSeedSessionReleaseTest(unittest.TestCase):
    def test_cli_is_dry_run_first_and_redacts_sensitive_arguments(self) -> None:
        self.assertFalse(cleanup_runner._arguments([]).apply)
        self.assertTrue(cleanup_runner._arguments(["--apply"]).apply)
        errors = io.StringIO()
        with redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            cleanup_runner._arguments(["--dsn=must-not-appear"])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("must-not-appear", errors.getvalue())

    def test_cleanup_uses_the_seed_uuid_contract_without_name_only_matching(
        self,
    ) -> None:
        environment = {
            "HERMES_SEED_TENANT_SLUG": "android-test",
            "HERMES_SEED_TENANT_DISPLAY_NAME": "Android Test",
            "HERMES_SEED_USERNAME": "android-user",
            "HERMES_SEED_USER_DISPLAY_NAME": "Android User",
            "HERMES_SEED_WORKSPACE_KEY": "android",
            "HERMES_SEED_WORKSPACE_DISPLAY_NAME": "Android",
            "HERMES_SEED_AGENT_KEY": "android-agent",
        }
        config = cleanup_runner.CleanupConfig.from_environment(environment)
        identity = cleanup_runner._seed_identity(config)
        namespace = UUID("ba84c827-b174-47f8-bbbd-52cbaf7232b9")
        expected = uuid5(
            namespace,
            "session\x1fandroid-test\x1fandroid\x1fandroid-bootstrap",
        )
        self.assertEqual(identity.session_id, expected)
        self.assertNotEqual(identity.session_id, UUID(int=0))


if __name__ == "__main__":
    unittest.main()
