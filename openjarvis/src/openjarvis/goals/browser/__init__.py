"""Fail-closed browser and official-provider boundary for durable goals.

This package contains no platform-specific scraping. Provider access is disabled
unless an official integration or an explicitly user-authorized browser session
is configured.
"""

from .policy import (
    ActionDecision,
    ActionKind,
    BrowserActionPolicy,
    ProviderAuthorization,
    ProviderConfig,
    PollIntervalGate,
    PolicyDecision,
)
from .providers import OfficialMcpProvider
from .seats import AdjacentSeatSelection, SeatMapEntry, select_adjacent_seats
from .session import BrowserObservation, BrowserSession, BrowserSessionManager

__all__ = [
    "ActionDecision",
    "ActionKind",
    "BrowserActionPolicy",
    "ProviderAuthorization",
    "ProviderConfig",
    "PollIntervalGate",
    "PolicyDecision",
    "OfficialMcpProvider",
    "AdjacentSeatSelection",
    "SeatMapEntry",
    "select_adjacent_seats",
    "BrowserObservation",
    "BrowserSession",
    "BrowserSessionManager",
]
