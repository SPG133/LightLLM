from __future__ import annotations

import time
from typing import List

from .base import MemoryScheduler


class FairPauseMemoryScheduler(MemoryScheduler):
    """
    Minimal fair-pause policy.

    A victim is eligible only if:
    1. its KV footprint is larger than `a * current_request_need`
    2. its wait ratio is not larger than the current average wait ratio
    """

    def _wait_ratio(self, req) -> float:
        now = time.time()
        if req.last_start_ts > 0:
            wait_time = max(0.0, req.last_start_ts - req.enqueue_ts)
        else:
            wait_time = max(0.0, now - req.enqueue_ts)
        return wait_time / max(1.0, self.estimate_standalone_latency(req))

    def select_victims(self, running_reqs: List, need_token_num: int, can_alloc_token_num: int) -> List:
        if need_token_num <= can_alloc_token_num:
            return []

        min_need = need_token_num * max(1.0, float(self.args.victim_min_ratio_to_need))
        current_avg_wait = self.avg_wait_ratio

        candidates = [
            req
            for req in running_reqs
            if req.cur_kv_len > min_need
            and not req.paused
            and not req.wait_pause
            and not req.finish_status.is_finished()
            and self._wait_ratio(req) <= current_avg_wait
        ]
        if not candidates:
            return []

        # Among eligible victims, choose the smallest strong task first.
        candidates.sort(key=lambda req: req.cur_kv_len)
        return [candidates[0]]
