from __future__ import annotations

import json
import math
import unittest
from dataclasses import FrozenInstanceError

from hermes_connector.bootstrap.config import ConnectorConfig
from hermes_connector.bootstrap.safe_logging import (
    LogCategory,
    LogState,
    SafeStructuredLogger,
)


class ConnectorConfigTest(unittest.TestCase):
    def test_safe_offline_defaults_are_explicit_and_bounded(self) -> None:
        config = ConnectorConfig()

        self.assertEqual(config.local_connect_timeout_seconds, 2.0)
        self.assertEqual(config.local_rpc_deadline_seconds, 3.0)
        self.assertEqual(config.local_max_reconnect_attempts, 3)
        self.assertEqual(config.local_reconnect_delay_seconds, 0.25)
        self.assertEqual(config.local_discovery_poll_interval_seconds, 5.0)
        self.assertEqual(config.cloud_heartbeat_interval_seconds, 20.0)
        self.assertEqual(config.start_deadline_seconds, 10.0)
        self.assertEqual(config.stop_deadline_seconds, 10.0)
        self.assertEqual(config.bounded_queue_items, 256)
        self.assertEqual(config.storage_write_deadline_seconds, 3.0)
        self.assertEqual(config.storage_busy_timeout_ms, 5_000)

    def test_config_is_immutable_and_rejects_non_positive_limits(self) -> None:
        config = ConnectorConfig()
        with self.assertRaises(FrozenInstanceError):
            config.bounded_queue_items = 1  # type: ignore[misc]

        for field_name, value in (
            ("local_connect_timeout_seconds", 0),
            ("local_rpc_deadline_seconds", -1),
            ("local_max_reconnect_attempts", 0),
            ("local_reconnect_delay_seconds", 0),
            ("local_discovery_poll_interval_seconds", 0),
            ("cloud_heartbeat_interval_seconds", 0),
            ("start_deadline_seconds", 0),
            ("stop_deadline_seconds", -1),
            ("bounded_queue_items", 0),
            ("storage_write_deadline_seconds", 0),
            ("storage_busy_timeout_ms", 0),
        ):
            with (
                self.subTest(field=field_name),
                self.assertRaises(ValueError),
            ):
                ConnectorConfig(**{field_name: value})

    def test_config_requires_finite_durations_and_strict_integer_counts(
        self,
    ) -> None:
        duration_fields = (
            "local_connect_timeout_seconds",
            "local_rpc_deadline_seconds",
            "local_reconnect_delay_seconds",
            "local_discovery_poll_interval_seconds",
            "cloud_heartbeat_interval_seconds",
            "start_deadline_seconds",
            "stop_deadline_seconds",
            "storage_write_deadline_seconds",
        )
        for field_name in duration_fields:
            for value in (math.nan, math.inf, -math.inf):
                with (
                    self.subTest(field=field_name, value=value),
                    self.assertRaises(ValueError),
                ):
                    ConnectorConfig(**{field_name: value})

        for field_name in (
            "local_max_reconnect_attempts",
            "bounded_queue_items",
            "storage_busy_timeout_ms",
        ):
            for value in (True, 1.5):
                with (
                    self.subTest(field=field_name, value=value),
                    self.assertRaises(ValueError),
                ):
                    ConnectorConfig(**{field_name: value})


class SafeStructuredLoggerTest(unittest.TestCase):
    def test_log_output_has_only_classification_component_and_state(self) -> None:
        lines: list[str] = []
        logger = SafeStructuredLogger(lines.append)

        logger.emit(
            category=LogCategory.LIFECYCLE,
            component="local_gateway",
            state=LogState.READY,
        )

        self.assertEqual(
            json.loads(lines[0]),
            {
                "category": "lifecycle",
                "component": "local_gateway",
                "state": "ready",
            },
        )
        self.assertNotIn("secret", lines[0])
        self.assertNotIn("payload", lines[0])

    def test_log_api_does_not_accept_secret_or_payload_fields(self) -> None:
        logger = SafeStructuredLogger(lambda _: None)

        with self.assertRaises(TypeError):
            logger.emit(  # type: ignore[call-arg]
                category=LogCategory.HEALTH,
                component="supervisor",
                state=LogState.FAILED,
                payload={"token": "must-not-be-logged"},
            )

    def test_component_identifier_must_be_a_safe_static_name(self) -> None:
        logger = SafeStructuredLogger(lambda _: None)

        for unsafe_name in ("", "contains space", "token=secret", "../payload"):
            with (
                self.subTest(component=unsafe_name),
                self.assertRaises(ValueError),
            ):
                logger.emit(
                    category=LogCategory.LIFECYCLE,
                    component=unsafe_name,
                    state=LogState.STARTING,
                )


if __name__ == "__main__":
    unittest.main()
