"""Shared error types for AI-SDLC framework."""

from __future__ import annotations


class ProjectNotInitializedError(Exception):
    """Raised when an operation requires an initialized project but none is found."""


class RefreshRequiredError(Exception):
    """Raised when a task cannot be marked completed because Knowledge Refresh is pending.

    Corresponds to BR-050: Level >= 1 blocks completion until refresh done.
    """


class GovernanceNotFrozenError(Exception):
    """Raised when an operation requires governance freeze but it has not been performed.

    Corresponds to BR-011.
    """
