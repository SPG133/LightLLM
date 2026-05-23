from __future__ import annotations

import time
from typing import List

from .base import MemoryScheduler


class FairPauseMemoryScheduler(MemoryScheduler):
    """
    First-stage memory scheduler.

    This version still uses pause + recover, but victim selection now
    considers release usefulness, recompute penalty, wait debt, and
    repeated-pause protection.
    """

    def _progress_score(self, req) -> float:
        output_budget = max(1, req.sampling_param.shm_param.max_new_tokens)
        output_progress = min(1.0, float(req.cur_output_len) / float(output_budget))
        kv_budget = max(1, req.shm_req.input_len + output_budget)
        kv_progress = min(1.0, float(req.cur_kv_len) / float(kv_budget))
        return 0.7 * output_progress + 0.3 * kv_progress

    def _recompute_penalty(self, req) -> float:
        progress = self._progress_score(req)
        standalone_latency = self.estimate_standalone_latency(req)
        return (0.6 * progress) + (0.4 * float(req.cur_kv_len) / standalone_latency)

    def _wait_debt(self, req) -> float:
        now = time.time()
        if req.paused:
            wait_time = max(0.0, now - req.enqueue_ts)
        elif req.last_start_ts > 0:
            wait_time = max(0.0, req.last_start_ts - req.enqueue_ts)
        else:
            wait_time = max(0.0, now - req.enqueue_ts)
        return wait_time / max(1.0, self.estimate_standalone_latency(req))

    def _resume_progress(self, req) -> int:
        return max(0, req.cur_output_len - req.output_tokens_at_resume)

    def _cooldown_penalty(self, req) -> float:
        if req.last_resume_ts <= 0:
            return 0.0
        since_resume = time.time() - req.last_resume_ts
        if since_resume < 0.5:
            return 10.0
        if since_resume < 2.0:
            return 3.0
        return 0.0

    def _starvation_penalty(self, req) -> float:
        penalty = 0.5 * float(req.pause_count)
        penalty += 2.5 * self._wait_debt(req)
        penalty += self._cooldown_penalty(req)
        if req.last_resume_ts > 0 and self._resume_progress(req) < 8:
            penalty += 3.0
        return penalty

    def _release_usefulness(self, req, gap: int, need_token_num: int) -> float:
        if gap <= 0:
            return 0.0
        released = float(req.cur_kv_len)
        min_ratio = max(1.0, float(self.args.victim_min_ratio_to_need))
        ratio = released / max(1.0, float(need_token_num))
        if ratio <= min_ratio:
            return -10.0
        if ratio <= min_ratio * 1.5:
            return 1.5
        if ratio <= min_ratio * 2.5:
            return 1.0
        return 0.3

    def _victim_score(self, req, gap: int, need_token_num: int) -> float:
        usefulness = self._release_usefulness(req, gap, need_token_num)
        recompute_penalty = self._recompute_penalty(req)
        starvation_penalty = self._starvation_penalty(req)
        return usefulness - recompute_penalty - starvation_penalty

    def select_victims(self, running_reqs: List, need_token_num: int, can_alloc_token_num: int) -> List:
        if need_token_num <= can_alloc_token_num:
            return []
        gap = need_token_num - can_alloc_token_num

        candidates = [
            req
            for req in running_reqs
            if req.cur_kv_len > 0 and not req.paused and not req.wait_pause and not req.finish_status.is_finished()
        ]
        if not candidates:
            return []

        # Avoid immediately re-pausing tasks that just resumed and have not
        # made meaningful progress yet.
        candidates = [req for req in candidates if self._cooldown_penalty(req) < 10.0]
        if not candidates:
            return []

        current_avg_wait = self.avg_wait_ratio
        min_need = need_token_num * max(1.0, float(self.args.victim_min_ratio_to_need))
        single_candidates = [
            req
            for req in candidates
            if req.cur_kv_len > min_need and self._wait_debt(req) <= current_avg_wait
        ]
        if single_candidates:
            single_candidates.sort(key=lambda req: self._victim_score(req, gap, need_token_num), reverse=True)
            if self._victim_score(single_candidates[0], gap, need_token_num) > -1.0:
                return [single_candidates[0]]

        # If no strong enough and sufficiently healthy single victim exists,
        # do not replace.
        return []
