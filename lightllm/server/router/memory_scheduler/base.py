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

    def select_victims(self, running_reqs: List, need_token_num: int, can_alloc_token_num: int) -> List:
        return []

    def select_resume_reqs(self, paused_reqs: List, can_alloc_token_num: int) -> List:
        return paused_reqs

    def can_use_swap_backend(self, req) -> bool:
        return self.args.enable_active_kv_swap and req.cur_kv_len >= self.args.swap_threshold_tokens

