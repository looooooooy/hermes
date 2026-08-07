from hermes_cloud.runtime_identity.runtime_projection import RuntimeProjectionFacade


class _Registry:
    pass


def test_runtime_projection_facade_constructs():
    facade = RuntimeProjectionFacade(_Registry())
    assert facade is not None
