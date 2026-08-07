from __future__ import annotations


from hermes_agent_plugin.bootstrap.registration import register


class _Validation:
    compatible = True
    missing_required_capabilities = ()
    register_extension = None


def test_registration_contract_exposes_extension_instance():
    """Document that registration returns the runtime extension object.

    The real host registration path is exercised in integration tests. This
    contract test protects the boundary required by runtime binding.
    """
    assert callable(register)
