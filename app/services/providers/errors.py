"""Provider failure types shared by search adapters."""

from __future__ import annotations


class SearchProviderError(Exception):
    """Base class for search provider failures.

    ``code`` is a stable snake_case identifier surfaced in the API error
    envelope. Messages must never include credentials or raw provider bodies.
    """

    code = "provider_error"
    http_status = 502


class ProviderConfigurationError(SearchProviderError):
    """The selected provider is missing required configuration."""

    code = "provider_not_configured"
    http_status = 503


class ProviderAuthenticationError(SearchProviderError):
    """The provider rejected the configured credentials."""

    code = "provider_authentication_failed"
    http_status = 502


class ProviderRateLimitError(SearchProviderError):
    """The provider rate limit was exceeded after retries."""

    code = "provider_rate_limited"
    http_status = 503


class ProviderTimeoutError(SearchProviderError):
    """The provider did not respond within the configured timeout."""

    code = "provider_timeout"
    http_status = 504


class ProviderUnavailableError(SearchProviderError):
    """The provider returned a server error or an unreadable response."""

    code = "provider_unavailable"
    http_status = 502
