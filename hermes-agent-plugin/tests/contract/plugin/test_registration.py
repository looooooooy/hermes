"""Source contracts for registration, compatibility, and product identity."""

from __future__ import annotations

import importlib
import importlib.resources
import inspect
import json
import re
import sys
import tomllib
from pathlib import Path

import pytest
from tests.test_support.core_host_spi_contract import (
    install_core_host_spi_contract,
)
from tests.test_support.host_spi_v1 import TEST_HOST_SPI_FACTORIES

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
RETIRED_IMPORT_SEGMENTS = ("hermes", "mobile", "gateway")
RETIRED_IMPORT_PACKAGE = "_".join(RETIRED_IMPORT_SEGMENTS)
sys.path.insert(0, str(PLUGIN_ROOT / "src"))


class _PluginContext:
    def __init__(
        self,
        capabilities=frozenset(
            {
                "audit.safe.v1",
                "extension.lifecycle.v1",
                "runtime.descriptor.v1",
                "session.observe.v1",
                "session.owner-actions.v1",
            }
        ),
    ) -> None:
        self.gateway_extension_spi_version = 1
        self.gateway_extension_capabilities = capabilities
        self.extension = None
        self.registered_spi_version = None

    def register_gateway_extension(self, extension, *, spi_version) -> None:
        self.extension = extension
        self.registered_spi_version = spi_version


class _MalformedCapabilities:
    def __iter__(self):
        raise RuntimeError("malformed capabilities")


class _CompleteCapabilitiesIterable:
    def __iter__(self):
        return iter(_PluginContext().gateway_extension_capabilities)


class _CapabilityFrozenset(frozenset):
    pass


def _diagnose(context):
    diagnostics_module = importlib.import_module("hermes_agent_plugin.diagnostics")
    return diagnostics_module.diagnose_host_context(context)


def test_register_exposes_one_canonical_extension(monkeypatch) -> None:
    plugin_module = importlib.import_module("hermes_agent_plugin")
    extension_module = importlib.import_module(
        "hermes_agent_plugin.adapters.host.extension"
    )
    registration_module = importlib.import_module(
        "hermes_agent_plugin.bootstrap.registration"
    )
    monkeypatch.setattr(
        registration_module,
        "load_public_host_spi_factories",
        lambda: TEST_HOST_SPI_FACTORIES,
    )
    context = _PluginContext()

    plugin_module.register(context)

    assert isinstance(
        context.extension,
        extension_module.HermesAgentPluginExtension,
    )
    assert context.registered_spi_version == 1


def test_register_rejects_host_missing_an_authoritative_owner_action_port() -> None:
    plugin_module = importlib.import_module("hermes_agent_plugin")
    context = _PluginContext(
        capabilities=frozenset(
            {
                "audit.safe.v1",
                "extension.lifecycle.v1",
                "runtime.descriptor.v1",
                "session.observe.v1",
            }
        )
    )

    with pytest.raises(
        plugin_module.HermesHostCompatibilityError,
        match="required capabilities are unavailable",
    ):
        plugin_module.register(context)

    assert context.extension is None


def test_register_rejects_a_different_gateway_extension_spi_version() -> None:
    plugin_module = importlib.import_module("hermes_agent_plugin")
    context = _PluginContext()
    context.gateway_extension_spi_version = 2

    with pytest.raises(plugin_module.HermesHostCompatibilityError):
        plugin_module.register(context)

    assert context.extension is None


def test_register_rejects_bool_spi_version_before_platform_composition(
    monkeypatch,
) -> None:
    registration_module = importlib.import_module(
        "hermes_agent_plugin.bootstrap.registration"
    )
    context = _PluginContext()
    context.gateway_extension_spi_version = True
    composed = False

    def configure_platform_adapters() -> None:
        nonlocal composed
        composed = True

    monkeypatch.setattr(
        registration_module,
        "configure_platform_adapters",
        configure_platform_adapters,
    )

    assert _diagnose(context).compatible is False
    with pytest.raises(registration_module.HermesHostCompatibilityError):
        registration_module.register(context)

    assert composed is False
    assert context.extension is None


def test_register_rejects_non_callable_entrypoint_before_platform_composition(
    monkeypatch,
) -> None:
    registration_module = importlib.import_module(
        "hermes_agent_plugin.bootstrap.registration"
    )
    context = _PluginContext()
    context.register_gateway_extension = object()
    composed = False

    def configure_platform_adapters() -> None:
        nonlocal composed
        composed = True

    monkeypatch.setattr(
        registration_module,
        "configure_platform_adapters",
        configure_platform_adapters,
    )

    assert _diagnose(context).compatible is False
    with pytest.raises(registration_module.HermesHostCompatibilityError):
        registration_module.register(context)

    assert composed is False
    assert context.extension is None


@pytest.mark.parametrize(
    "capabilities",
    [
        pytest.param(
            _PluginContext().gateway_extension_capabilities | {1},
            id="non-string-member",
        ),
        pytest.param(_MalformedCapabilities(), id="malformed-iterable"),
    ],
)
def test_register_rejects_invalid_capabilities_before_platform_composition(
    monkeypatch,
    capabilities,
) -> None:
    registration_module = importlib.import_module(
        "hermes_agent_plugin.bootstrap.registration"
    )
    context = _PluginContext(capabilities=capabilities)
    composed = False

    def configure_platform_adapters() -> None:
        nonlocal composed
        composed = True

    monkeypatch.setattr(
        registration_module,
        "configure_platform_adapters",
        configure_platform_adapters,
    )

    assert _diagnose(context).compatible is False
    with pytest.raises(registration_module.HermesHostCompatibilityError):
        registration_module.register(context)

    assert composed is False
    assert context.extension is None


@pytest.mark.parametrize(
    "capabilities",
    [
        pytest.param(
            dict.fromkeys(_PluginContext().gateway_extension_capabilities, True),
            id="mapping",
        ),
        pytest.param(
            list(_PluginContext().gateway_extension_capabilities),
            id="list",
        ),
        pytest.param(
            tuple(_PluginContext().gateway_extension_capabilities),
            id="tuple",
        ),
        pytest.param(
            "session.observe.v1",
            id="string",
        ),
        pytest.param(
            set(_PluginContext().gateway_extension_capabilities),
            id="mutable-set",
        ),
        pytest.param(_CompleteCapabilitiesIterable(), id="arbitrary-iterable"),
        pytest.param(
            _CapabilityFrozenset(_PluginContext().gateway_extension_capabilities),
            id="frozenset-subclass",
        ),
    ],
)
def test_register_requires_exact_frozenset_capability_shape(
    monkeypatch,
    capabilities,
) -> None:
    registration_module = importlib.import_module(
        "hermes_agent_plugin.bootstrap.registration"
    )
    context = _PluginContext(capabilities=capabilities)
    composed = False

    def configure_platform_adapters() -> None:
        nonlocal composed
        composed = True

    monkeypatch.setattr(
        registration_module,
        "configure_platform_adapters",
        configure_platform_adapters,
    )

    report = _diagnose(context)
    assert report.reason == "missing_context_members"
    assert report.missing_context_members == ("gateway_extension_capabilities",)
    with pytest.raises(registration_module.HermesHostCompatibilityError):
        registration_module.register(context)

    assert composed is False
    assert context.extension is None


def test_register_checks_host_compatibility_before_platform_composition(
    monkeypatch,
) -> None:
    registration_module = importlib.import_module(
        "hermes_agent_plugin.bootstrap.registration"
    )
    composed = False

    def configure_platform_adapters() -> None:
        nonlocal composed
        composed = True

    monkeypatch.setattr(
        registration_module,
        "configure_platform_adapters",
        configure_platform_adapters,
    )

    with pytest.raises(registration_module.HermesHostCompatibilityError):
        registration_module.register(object())

    assert composed is False


@pytest.mark.parametrize(
    "malformed_name",
    (
        "ObserverRequest",
        "ControlScope",
        "OwnerActionRequest",
        "SafeAuditEvent",
    ),
)
def test_register_rejects_malformed_public_dto_constructors_before_composition(
    monkeypatch,
    malformed_name: str,
) -> None:
    registration_module = importlib.import_module(
        "hermes_agent_plugin.bootstrap.registration"
    )
    public_module = install_core_host_spi_contract()
    setattr(public_module, malformed_name, lambda: object())
    context = _PluginContext()
    composed = False

    def configure_platform_adapters() -> None:
        nonlocal composed
        composed = True

    monkeypatch.setattr(
        registration_module,
        "configure_platform_adapters",
        configure_platform_adapters,
    )

    with pytest.raises(registration_module.HermesHostCompatibilityError):
        registration_module.register(context)

    assert composed is False
    assert context.extension is None


def test_public_api_exposes_only_the_canonical_extension() -> None:
    plugin_module = importlib.import_module("hermes_agent_plugin")
    extension_module = importlib.import_module(
        "hermes_agent_plugin.adapters.host.extension"
    )
    extension_class = extension_module.HermesAgentPluginExtension

    assert plugin_module.HermesAgentPluginExtension is extension_class
    assert plugin_module.register


def test_production_spi_module_contains_no_instantiable_shadow_core_dtos() -> None:
    spi_module = importlib.import_module("hermes_agent_plugin.adapters.host.spi_v1")

    for forbidden in (
        "ObserverRequest",
        "ControlScope",
        "OwnerActionRequest",
        "SafeAuditEvent",
        "LOCAL_HOST_SPI_FACTORIES",
        "_LOCAL_HOST_SPI_FACTORIES",
    ):
        assert forbidden not in spi_module.__all__
        assert not hasattr(spi_module, forbidden)


def test_public_observer_request_defaults_and_v2_behavior_are_exact() -> None:
    spi_module = importlib.import_module("hermes_agent_plugin.adapters.host.spi_v1")
    public_module = install_core_host_spi_contract()

    factories = spi_module.load_public_host_spi_factories()
    v1 = factories.observer_request(
        profile="default",
        durable_session_key="session-1",
        runtime_generation="generation-1",
    )
    v2 = factories.observer_request(
        profile="default",
        durable_session_key="session-1",
        runtime_generation="generation-1",
        observer_contract=2,
        required_capabilities=frozenset({"session.observe.output-parity.v1"}),
    )

    assert type(v1) is public_module.ObserverRequest
    assert v1.observer_contract == 1
    assert type(v1.required_capabilities) is frozenset
    assert v1.required_capabilities == frozenset()
    assert type(v2) is public_module.ObserverRequest
    assert v2.observer_contract == 2
    assert v2.required_capabilities == frozenset({"session.observe.output-parity.v1"})


def test_public_host_spi_loader_accepts_a_pre_catalog_v1_dto_module() -> None:
    spi_module = importlib.import_module("hermes_agent_plugin.adapters.host.spi_v1")
    public_module = install_core_host_spi_contract()
    delattr(public_module, "SessionCatalogRequest")

    factories = spi_module.load_public_host_spi_factories()

    assert factories.session_catalog_request is None
    assert factories.observer_request is public_module.ObserverRequest
    assert factories.control_scope is public_module.ControlScope
    assert factories.owner_action_request is public_module.OwnerActionRequest
    assert factories.safe_audit_event is public_module.SafeAuditEvent


def test_public_host_spi_loader_uses_the_real_catalog_dto_when_published() -> None:
    spi_module = importlib.import_module("hermes_agent_plugin.adapters.host.spi_v1")
    public_module = install_core_host_spi_contract()

    factories = spi_module.load_public_host_spi_factories()

    assert factories.session_catalog_request is public_module.SessionCatalogRequest


def test_public_host_spi_loader_rejects_a_malformed_published_catalog_dto() -> None:
    spi_module = importlib.import_module("hermes_agent_plugin.adapters.host.spi_v1")
    public_module = install_core_host_spi_contract()
    public_module.SessionCatalogRequest = lambda: object()

    with pytest.raises(
        spi_module.PublicHostSpiContractUnavailable,
        match="public Host SPI v1 DTO module is unavailable",
    ):
        spi_module.load_public_host_spi_factories()


@pytest.mark.parametrize(
    "malformed_declaration",
    (
        "observer_contract: int = 2\n"
        "    required_capabilities: frozenset[str] = field(default_factory=frozenset)",
        "observer_contract: int = 1\n"
        "    required_capabilities: frozenset[str] = field(default_factory=list)",
    ),
)
def test_public_observer_request_rejects_malformed_defaults(
    malformed_declaration: str,
) -> None:
    spi_module = importlib.import_module("hermes_agent_plugin.adapters.host.spi_v1")
    public_module = install_core_host_spi_contract()
    exec(
        "@dataclass(frozen=True)\n"
        "class ObserverRequest:\n"
        "    profile: str\n"
        "    durable_session_key: str\n"
        "    runtime_generation: str\n"
        f"    {malformed_declaration}\n",
        public_module.__dict__,
    )

    with pytest.raises(
        spi_module.PublicHostSpiContractUnavailable,
        match="public Host SPI v1 DTO module is unavailable",
    ):
        spi_module.load_public_host_spi_factories()


@pytest.mark.parametrize("invalid_version", (True, 1.0, 2))
def test_public_spi_version_is_rejected_before_constructor_access(
    monkeypatch,
    invalid_version: object,
) -> None:
    spi_module = importlib.import_module("hermes_agent_plugin.adapters.host.spi_v1")
    touched: list[str] = []

    class VersionFirstModule:
        GATEWAY_EXTENSION_SPI_VERSION = invalid_version

        def __getattribute__(self, name: str) -> object:
            if name in {
                "ObserverRequest",
                "ControlScope",
                "OwnerActionRequest",
                "SafeAuditEvent",
            }:
                touched.append(name)
                raise AssertionError("DTO constructor touched before version rejection")
            return object.__getattribute__(self, name)

    monkeypatch.setattr(
        spi_module.importlib,
        "import_module",
        lambda _name: VersionFirstModule(),
    )

    with pytest.raises(spi_module.PublicHostSpiContractUnavailable):
        spi_module.load_public_host_spi_factories()

    assert touched == []


def test_bootstrap_public_api_only_registers_with_the_running_host() -> None:
    bootstrap_module = importlib.import_module("hermes_agent_plugin.bootstrap")

    assert bootstrap_module.__all__ == ["register"]
    for retired_name in (
        "GatewayBootstrap",
        "HermesAgentPluginRuntime",
        "create_platform_local_gateway_resource",
        "create_production_control_relay_resource",
    ):
        assert not hasattr(bootstrap_module, retired_name)


def test_retired_standalone_runtime_fails_before_creating_resources() -> None:
    runtime_module = importlib.import_module("hermes_agent_plugin.bootstrap.runtime")

    assert hasattr(runtime_module, "StandaloneRuntimeProhibited")
    error_type = runtime_module.StandaloneRuntimeProhibited
    expected = (
        "standalone_plugin_runtime_prohibited: load hermes-agent-plugin through "
        "the running Hermes Agent PluginManager; gateway-extension/1 is required"
    )
    with pytest.raises(error_type, match=f"^{re.escape(expected)}$"):
        runtime_module.HermesAgentPluginRuntime()
    with pytest.raises(error_type, match=f"^{re.escape(expected)}$"):
        runtime_module.create_platform_local_gateway_resource()
    with pytest.raises(error_type, match=f"^{re.escape(expected)}$"):
        runtime_module.create_production_control_relay_resource()


def test_extension_uses_only_the_frozen_host_facade_v1_surface() -> None:
    extension_module = importlib.import_module(
        "hermes_agent_plugin.adapters.host.extension"
    )
    source = inspect.getsource(extension_module)

    assert "register_connection_role" not in source
    assert "register_local_endpoint" in source
    assert "invoke_owner_action" in source
    assert "prepare_observer" in source
    assert "open_observer" not in source
    assert "control_snapshot" in source


def test_distribution_and_manifest_declare_only_canonical_identity() -> None:
    configuration = tomllib.loads(
        (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = configuration["project"]
    manifest = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")

    assert project["name"] == "hermes-agent-plugin"
    assert project["entry-points"]["hermes_agent.plugins"] == {
        "hermes-agent-plugin": "hermes_agent_plugin"
    }
    assert project["dependencies"] == ["websockets>=13,<17"]
    assert project["optional-dependencies"]["hermes-019-contract-test"] == [
        "hermes-agent==0.19.0"
    ]
    assert "mobile" not in project["description"].lower()
    assert all("mobile" not in author["name"].lower() for author in project["authors"])
    assert project["readme"]["content-type"] == "text/plain"
    assert "mobile" not in project["readme"]["text"].lower()
    assert "name: hermes-agent-plugin" in manifest
    assert (
        "description: Hermes Agent extension for observer and "
        "explicit-control connections."
    ) in manifest
    assert "entry_point: hermes_agent_plugin:register" in manifest
    assert 'requires_host_spi: "gateway-extension/1"' in manifest
    assert 'known_incompatible_hermes: ">=0.19,<0.21"' in manifest
    assert "requires_hermes:" not in manifest
    assert "hermes_agent_plugin:" in manifest
    assert "hermes_mobile:" not in manifest
    assert "control_contract: contracts/generated/mobile-control-v1.json" in manifest
    assert (
        "observer_output_parity_contract: "
        "contracts/generated/observer-output-parity-v2.json"
    ) in manifest
    assert (
        "observer_event_schema: "
        "contracts/generated/schemas/cloud/payloads/session-event-v2.schema.json"
    ) in manifest
    assert (
        "observer_snapshot_schema: "
        "contracts/generated/schemas/cloud/payloads/session-snapshot-v2.schema.json"
    ) in manifest
    assert (
        "session_catalog_contract: contracts/generated/session-catalog-v1.json"
        in manifest
    )
    assert (
        "session_catalog_rpc_schema: "
        "contracts/generated/schemas/local/session-catalog-rpc-v1.schema.json"
    ) in manifest
    assert (
        "session_catalog_entry_schema: "
        "contracts/generated/schemas/session-catalog-entry-v1.schema.json"
    ) in manifest
    assert "local_gateway_paths:" in manifest
    assert "require_distinct_directories: true" in manifest
    assert (
        "local_gateway_registry_directory: HERMES_LOCAL_GATEWAY_REGISTRY_DIR"
        in manifest
    )
    assert "local_gateway_socket_directory: HERMES_LOCAL_GATEWAY_SOCKET_DIR" in manifest
    assert "control_registry_directory: HERMES_CONTROL_REGISTRY_DIR" in manifest
    assert "control_socket_directory: HERMES_CONTROL_SOCKET_DIR" in manifest
    assert "observer_registry_directory: HERMES_OBSERVER_REGISTRY_DIR" in manifest
    assert "observer_socket_directory: HERMES_OBSERVER_SOCKET_DIR" in manifest
    assert configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "src/hermes_agent_plugin"
    ]
    assert not (PLUGIN_ROOT / "src" / RETIRED_IMPORT_PACKAGE).exists()


def test_canonical_tree_allows_only_frozen_compatibility_resource_name() -> None:
    package_root = PLUGIN_ROOT / "src/hermes_agent_plugin"
    allowed_relative_path = Path("contracts/generated/mobile-control-v1.json")
    forbidden_product_identifiers = (
        "hermesmobile",
        "hermes mobile",
        "hermes_mobile",
        "hermes-mobile",
        "mobile gateway",
        "mobile_gateway",
        "mobile-gateway",
    )
    frozen_external_identifiers = {
        Path("domain/control_lease.py"): frozenset({"hermes mobile"}),
    }
    source_files = sorted(
        path
        for path in package_root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".json"}
    )
    relative_paths = {path.relative_to(package_root) for path in source_files}

    assert allowed_relative_path in relative_paths
    for source_file in source_files:
        relative_path = source_file.relative_to(package_root)
        if relative_path == allowed_relative_path:
            continue
        assert "mobile" not in relative_path.as_posix().lower()
        source_text = source_file.read_text(encoding="utf-8").lower()
        unexpected_identifiers = {
            identifier
            for identifier in forbidden_product_identifiers
            if identifier in source_text
            and identifier not in frozen_external_identifiers.get(relative_path, ())
        }
        assert unexpected_identifiers == set(), relative_path


def test_packaged_contract_copies_match_authoritative_core_contract() -> None:
    canonical_contract = json.loads(
        importlib.resources.files("hermes_agent_plugin.contracts.generated")
        .joinpath("mobile-control-v1.json")
        .read_text(encoding="utf-8")
    )
    core_contract = json.loads(
        (PLUGIN_ROOT.parent / "contracts/sources/mobile-control-v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert canonical_contract == core_contract
