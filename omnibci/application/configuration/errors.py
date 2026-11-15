"""Configuration errors shared by storage paths and runtime admission."""


class ConfigProfileValidationError(ValueError):
    """A profile, selection, or parameter is invalid or unsupported."""


class ConfigProfileConflictError(RuntimeError):
    """A profile operation conflicts with a name or current runtime state."""


class PluginProfileValidationError(ValueError):
    """A profile, binding or stored activation is invalid."""


class PluginProfileNotFoundError(KeyError):
    """The requested profile does not exist."""


class ProfileRevisionConflict(RuntimeError):
    """The stored document changed after the caller read it."""


class PluginProfileStorageError(OSError):
    """Local plugin configuration could not be read or written."""


class ConfigKeyNotAllowedError(KeyError):
    def __init__(self, keys: list[str]):
        self.keys = keys
        super().__init__(f"Keys not in whitelist: {keys}")


class ConfigValidationError(ValueError):
    def __init__(self, bad_keys: dict[str, str]):
        self.bad_keys = bad_keys
        super().__init__(f"Invalid config values: {sorted(bad_keys)}")
