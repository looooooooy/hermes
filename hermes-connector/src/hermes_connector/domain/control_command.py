from __future__ import annotations


class LocalControlFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class LocalControlOutcomeUnknown(RuntimeError):
    pass
