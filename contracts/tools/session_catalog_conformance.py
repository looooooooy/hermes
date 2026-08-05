"""Semantic conformance checks for the session catalog replication protocol."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


IDENTITY_FIELDS = {"agent_id", "tenant_id", "device_id"}
CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
CONTRACT_ROOT = Path(__file__).resolve().parents[1]
VECTOR_SCHEMA = json.loads(
    (
        CONTRACT_ROOT
        / "schemas/conformance/session-catalog-semantic-vector-v1.schema.json"
    ).read_text(encoding="utf-8")
)
VECTOR_VALIDATOR = Draft202012Validator(
    VECTOR_SCHEMA,
    format_checker=FormatChecker(),
)


def _contains_identity(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(IDENTITY_FIELDS.intersection(value)) or any(
            _contains_identity(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_identity(item) for item in value)
    return False


def _append_once(errors: list[str], error: str) -> None:
    if error not in errors:
        errors.append(error)


def _merge_errors(errors: list[str], operation_errors: list[str]) -> None:
    for error in operation_errors:
        _append_once(errors, error)


def validate_vector(vector: object) -> list[str]:
    """Run a deterministic catalog state machine and return semantic errors."""

    if list(VECTOR_VALIDATOR.iter_errors(vector)):
        return ["invalid_vector"]

    assert isinstance(vector, dict)
    state = deepcopy(vector.get("initial_state", {}))
    assert isinstance(state, dict)
    state.setdefault("retired_runtime_generations", [])
    state.setdefault("catalog_session_keys", [])
    state.setdefault(
        "catalog_entries",
        {session_key: 0 for session_key in state["catalog_session_keys"]},
    )
    state.setdefault("stable_session_ids", {})
    if set(state["catalog_entries"]) != set(state["catalog_session_keys"]):
        return ["catalog_index_mismatch"]
    errors: list[str] = []

    for operation in vector.get("operations", []):
        assert isinstance(operation, dict)
        writer_matches = (
            operation.get("writer_id") == state.get("current_writer_id")
            and operation.get("writer_fence") == state.get("current_writer_fence")
        )
        op = operation.get("op")

        if op in {"snapshot", "event"} and not writer_matches:
            _append_once(errors, "stale_writer")
            continue

        if op == "snapshot":
            operation_errors: list[str] = []
            pages = operation.get("pages", [])
            if not pages:
                _append_once(operation_errors, "snapshot_page_missing")
                _merge_errors(errors, operation_errors)
                continue
            first = pages[0]
            profile = first.get("profile")
            generation = first.get("runtime_generation")
            if generation in state["retired_runtime_generations"]:
                _append_once(operation_errors, "late_old_generation")
                _merge_errors(errors, operation_errors)
                continue
            snapshot_id = first.get("snapshot_id")
            revision = first.get("catalog_revision")
            session_keys: list[Any] = []
            catalog_entries: dict[Any, Any] = {}

            for expected_index, page in enumerate(pages):
                if page.get("page_index") != expected_index:
                    _append_once(operation_errors, "page_index_gap")
                if (
                    page.get("profile") != profile
                    or page.get("runtime_generation") != generation
                    or page.get("snapshot_id") != snapshot_id
                ):
                    _append_once(operation_errors, "snapshot_scope_changed")
                if page.get("catalog_revision") != revision:
                    _append_once(operation_errors, "snapshot_revision_changed")
                should_be_last = expected_index == len(pages) - 1
                if page.get("is_last") is not should_be_last:
                    _append_once(operation_errors, "snapshot_terminal_mismatch")
                for entry in page.get("sessions", []):
                    session_key = entry.get("session_key")
                    if session_key in session_keys:
                        _append_once(operation_errors, "duplicate_session_key")
                    session_keys.append(session_key)
                    catalog_entries[session_key] = entry.get("authority_revision")
                    if _contains_identity(entry):
                        _append_once(operation_errors, "self_asserted_identity")

            # Snapshot installation is atomic: no state changes until every page passes.
            if not operation_errors:
                old_generation = state.get("active_runtime_generation")
                if old_generation and old_generation != generation:
                    retired = state["retired_runtime_generations"]
                    if old_generation not in retired:
                        retired.append(old_generation)
                state["profile"] = profile
                state["active_runtime_generation"] = generation
                state["catalog_revision"] = revision
                state["catalog_sequence"] = revision
                state["catalog_session_keys"] = session_keys
                state["catalog_entries"] = catalog_entries
            _merge_errors(errors, operation_errors)

        elif op == "event":
            generation = operation.get("runtime_generation")
            if generation in state["retired_runtime_generations"]:
                _append_once(errors, "late_old_generation")
                continue
            if generation != state.get("active_runtime_generation"):
                _append_once(errors, "runtime_generation_mismatch")
                continue
            if operation.get("profile") != state.get("profile"):
                _append_once(errors, "event_scope_changed")
                continue
            expected_sequence = state.get("catalog_sequence", 0) + 1
            if operation.get("catalog_sequence") != expected_sequence:
                _append_once(errors, "event_sequence_gap")
                continue
            if _contains_identity(operation.get("entry", {})):
                _append_once(errors, "self_asserted_identity")
                continue
            action = operation.get("action")
            if action not in {"upsert", "remove"}:
                _append_once(errors, "unknown_action")
                continue
            entry = operation["entry"]
            session_key = entry["session_key"]
            authority_revision = entry["authority_revision"]
            stored_revision = state["catalog_entries"].get(session_key)
            if action == "upsert":
                if stored_revision is not None and authority_revision < stored_revision:
                    _append_once(errors, "stale_authority_revision")
                    continue
                state["catalog_entries"][session_key] = authority_revision
                if session_key not in state["catalog_session_keys"]:
                    state["catalog_session_keys"].append(session_key)
            else:
                if stored_revision is None:
                    _append_once(errors, "remove_target_missing")
                    continue
                if authority_revision != stored_revision:
                    _append_once(errors, "remove_revision_mismatch")
                    continue
                if session_key not in state["catalog_session_keys"]:
                    _append_once(errors, "catalog_index_mismatch")
                    continue
                del state["catalog_entries"][session_key]
                state["catalog_session_keys"].remove(session_key)
            state["catalog_sequence"] = operation["catalog_sequence"]

        elif op == "map_identity":
            agent_id = operation.get("authenticated_agent_id")
            profile = operation.get("profile")
            session_key = operation.get("session_key")
            session_id = operation.get("session_id")
            if (
                not isinstance(agent_id, str)
                or CANONICAL_UUID.fullmatch(agent_id) is None
                or not isinstance(profile, str)
                or re.fullmatch(r"[A-Za-z0-9_.-]+", profile) is None
                or not isinstance(session_key, str)
                or not session_key
            ):
                _append_once(errors, "stable_session_scope_missing")
                continue
            if not isinstance(session_id, str) or CANONICAL_UUID.fullmatch(
                session_id
            ) is None:
                _append_once(errors, "invalid_stable_session_id")
                continue
            scope_key = f"{agent_id}|{profile}|{session_key}"
            mapped = state["stable_session_ids"].get(scope_key)
            if mapped is not None and mapped != session_id:
                _append_once(errors, "stable_session_id_changed")
            else:
                state["stable_session_ids"][scope_key] = session_id

        elif op == "assert_state":
            for field, expected in operation.get("equals", {}).items():
                if state.get(field) != expected:
                    _append_once(errors, f"state_mismatch:{field}")

    return errors
