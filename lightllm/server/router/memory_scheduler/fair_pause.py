from __future__ import annotations

from typing import List

from .base import MemoryScheduler


class FairPauseMemoryScheduler(MemoryScheduler):
    """
    First-stage memory scheduler.

    This version does not move active KV to CPU yet. It only chooses better
    pause victims than the default "current request loses" behavior.
    """

    def _victim_score(self, req) -> float:
        return float(req.cur_kv_len) + 0.25 * float(req.cur_output_len)

    def select_victims(self, running_reqs: List, need_token_num: int, can_alloc_token_num: int) -> List:
        if need_token_num <= can_alloc_token_num:
            return []

        candidates = [
            req
            for req in running_reqs
            if req.cur_kv_len > 0 and not req.paused and not req.wait_pause and not req.finish_status.is_finished()
        ]
        candidates.sort(key=self._victim_score, reverse=True)

        released = 0
        victims = []
        for req in candidates:
            victims.append(req)
            released += req.cur_kv_len
            if can_alloc_token_num + released >= need_token_num:
                break
        return victims

