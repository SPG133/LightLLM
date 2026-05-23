from __future__ import annotations

from typing import List


class MemoryScheduler:
    """
    Memory-aware scheduling interface.

    The first stage only coordinates victim selection for pause-based reclaim.
    Active KV swap can be layered on top later.
    """

    def __init__(self, args) -> None:
        self.args = args
        self.avg_wait_ratio = 0.0
        self.finished_req_count = 0
        self.weighted_wait_ratio_sum = 0.0
        self.total_execution_weight = 0.0

    def select_victims(self, running_reqs: List, need_token_num: int, can_alloc_token_num: int) -> List:
        return []

    def select_resume_reqs(self, paused_reqs: List, can_alloc_token_num: int) -> List:
        return paused_reqs

    def can_use_swap_backend(self, req) -> bool:
        return self.args.enable_active_kv_swap and req.cur_kv_len >= self.args.swap_threshold_tokens

    def estimate_standalone_latency(self, req) -> float:
        prompt_tokens = max(1, req.shm_req.input_len)
        output_budget = max(1, req.sampling_param.shm_param.max_new_tokens)
        return float(prompt_tokens + output_budget)

    def get_wait_ratio(self, req) -> float:
        return req.total_wait_time / max(1.0, self.estimate_standalone_latency(req))

    def on_req_finished(self, req) -> None:
        import time

        req.finish_ts = time.time()
        if req.last_start_ts > 0:
            req.last_execution_time = max(1e-6, req.finish_ts - req.last_start_ts)
        else:
            req.last_execution_time = 1e-6
        wait_ratio = self.get_wait_ratio(req)
        self.finished_req_count += 1
        self.weighted_wait_ratio_sum += wait_ratio * req.last_execution_time
        self.total_execution_weight += req.last_execution_time
        self.avg_wait_ratio = self.weighted_wait_ratio_sum / max(1e-6, self.total_execution_weight)
