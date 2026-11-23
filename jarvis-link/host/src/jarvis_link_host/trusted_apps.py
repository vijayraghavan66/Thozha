"""Narrow allowlist for no-prompt foreground app activation.

Only exact display aliases and canonical macOS bundle IDs are trusted. This
module resolves identifiers; it does not launch applications or grant tool
authority. The privileged Mac executor must still confirm installation and
activation and return a verifiable outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class TrustedApp:
    display_name: str
    bundle_id: str


_APPS = (
    TrustedApp("Calculator", "com.apple.calculator"),
    TrustedApp("Spotify", "com.spotify.client"),
)
_TRUSTED_BY_ALIAS = {
    key: app
    for app in _APPS
    for key in (app.display_name, app.bundle_id)
}

# Fallback input is only a plain app label. Paths, flags, shell syntax,
# control characters, and arbitrary bundle IDs are not accepted.
_SAFE_APP_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ]{0,63}$")


def resolve_trusted_app(value: object) -> TrustedApp | None:
    """Resolve one exact display alias or canonical bundle ID."""
    if not isinstance(value, str):
        return None
    return _TRUSTED_BY_ALIAS.get(value)


def is_safe_untrusted_app_label(value: object) -> bool:
    """Whether an unknown target can be forwarded as inert text for normal review.

    This is not permission to launch the target. The caller must send this only
    through the ordinary assistant/supervisor route, which retains operation
    approvals and the normal tool sandbox.
    """
    return (
        isinstance(value, str)
        and value == value.strip()
        and _SAFE_APP_LABEL.fullmatch(value) is not None
    )
