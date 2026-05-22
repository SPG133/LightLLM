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

    def _estimate_standalone_latency(self, req) -> float:
        prompt_tokens = max(1, req.shm_req.input_len)
        output_budget = max(1, req.sampling_param.shm_param.max_new_tokens)
        return float(prompt_tokens + output_budget)

    def _progress_score(self, req) -> float:
        output_budget = max(1, req.sampling_param.shm_param.max_new_tokens)
        output_progress = min(1.0, float(req.cur_output_len) / float(output_budget))
        kv_budget = max(1, req.shm_req.input_len + output_budget)
        kv_progress = min(1.0, float(req.cur_kv_len) / float(kv_budget))
        return 0.7 * output_progress + 0.3 * kv_progress

    def _recompute_penalty(self, req) -> float:
        progress = self._progress_score(req)
        standalone_latency = self._estimate_standalone_latency(req)
        return (0.6 * progress) + (0.4 * float(req.cur_kv_len) / standalone_latency)

    def _wait_debt(self, req) -> float:
        now = time.time()
        current_wait = 0.0
        if req.paused and req.last_pause_ts > 0:
            current_wait = max(0.0, now - req.last_pause_ts)
        return (req.total_wait_time + current_wait) / max(1.0, self._estimate_standalone_latency(req))

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

    def _release_usefulness(self, req, gap: int) -> float:
        if gap <= 0:
            return 0.0
        released = float(req.cur_kv_len)
        if released < gap:
            return 0.2 * (released / gap)
        if released <= 2 * gap:
            return 1.0 - ((released - gap) / max(1.0, gap))
        return -0.5 * ((released - 2 * gap) / max(1.0, gap))

    def _victim_score(self, req, gap: int) -> float:
        usefulness = self._release_usefulness(req, gap)
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

        # First try a single-victim best candidate that covers the gap and
        # still offers acceptable net gain.
        single_candidates = [req for req in candidates if req.cur_kv_len >= gap]
        if single_candidates:
            single_candidates.sort(key=lambda req: self._victim_score(req, gap), reverse=True)
            if self._victim_score(single_candidates[0], gap) > -1.0:
                return [single_candidates[0]]

        # Otherwise greedily accumulate candidates with the highest net gain.
        candidates.sort(key=lambda req: self._victim_score(req, gap), reverse=True)

        released = 0
        victims = []
        for req in candidates:
            if self._victim_score(req, gap) <= -2.0:
                continue
            victims.append(req)
            released += req.cur_kv_len
            if can_alloc_token_num + released >= need_token_num:
                break
        return victims
