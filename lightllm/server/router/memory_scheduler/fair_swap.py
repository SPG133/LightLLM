from __future__ import annotations

from .fair_pause import FairPauseMemoryScheduler


class FairSwapMemoryScheduler(FairPauseMemoryScheduler):
    """
    Placeholder for the second-stage scheduler.

    The active KV swap backend is not wired yet. For now it falls back to the
    same victim policy as fair_pause while exposing a separate scheduler mode.
    """

    pass

