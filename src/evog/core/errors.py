"""Public errors without provider response bodies or credentials."""


class EvoGError(Exception):
    """Base error for callers and the CLI."""


class ProviderError(EvoGError):
    """A request failed or the provider returned an invalid response."""


class DeadlineExceeded(EvoGError):
    """The interaction's wall-clock deadline has elapsed."""


class ContractError(EvoGError):
    """Data violates a runtime or evidence contract."""


class ConflictError(EvoGError):
    """An immutable record or revision cannot be overwritten."""


class RunFailed(EvoGError):
    def __init__(self, run_id: str, reason: str):
        self.run_id = run_id
        super().__init__(f"Run {run_id} failed: {reason}")
