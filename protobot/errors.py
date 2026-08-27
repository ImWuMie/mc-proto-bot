"""ProtoBot exception hierarchy."""


class ProtoBotError(Exception):
    """Base exception for the library."""


class ProtocolError(ProtoBotError):
    """The peer sent malformed or unexpected protocol data."""


class ConnectionClosed(ProtoBotError):
    """The network connection closed before the requested operation completed."""


class LoginRejected(ProtoBotError):
    """The server rejected the login."""


class OnlineModeRequired(LoginRejected):
    """The server requested online-mode encryption but credentials or crypto support are missing."""


class AuthenticationError(LoginRejected):
    """Authentication with the session server or Microsoft identity provider failed."""


class UnsupportedVersion(ProtoBotError, ValueError):
    """The requested Minecraft release is not in the supported version matrix."""
