"""Configuration errors shared by legacy profiles and runtime admission."""


class ConfigProfileValidationError(ValueError):
    """A profile, selection, or parameter is invalid or unsupported."""


class ConfigProfileConflictError(RuntimeError):
    """A profile operation conflicts with a name or current runtime state."""
