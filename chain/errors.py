"""Configuration failures with stable, non-secret diagnostic locations."""
import re


class ConfigError(ValueError):
    def __init__(self, code, path, message, *, source=None):
        self.code = code
        self.path = path
        self.source = source
        self.message = message
        location = f"{source}:{path}" if source else path
        super().__init__(f"{location} [{code}] {message}")


class UnsupportedSchema(ConfigError):
    """An explicitly unsupported authoring format or version."""


class BackendUnavailable(Exception):
    """Explicit backend transport failure with stable, non-secret diagnostics.

    Keep this shared class outside installed entrypoint modules, whose pinned
    bytes may be executed under a private module name. An application client
    and an installed provider must recognize the same exception class.
    Schema, identity and version failures are not backend unavailability.
    """
    def __init__(self, code="BACKEND_UNAVAILABLE", *, retryable=True):
        if (not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code)
                or type(retryable) is not bool):
            raise ConfigError("BACKEND_ERROR_INVALID", "backend_error",
                              "Expected a stable error code and boolean retryability") from None
        self.code, self.retryable = code, retryable
        super().__init__(code)
