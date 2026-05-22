from .base import MemoryScheduler
from .fair_pause import FairPauseMemoryScheduler
from .fair_swap import FairSwapMemoryScheduler


def build_memory_scheduler(args):
    if args.mem_scheduler == "fair_pause":
        return FairPauseMemoryScheduler(args)
    if args.mem_scheduler == "fair_swap":
        return FairSwapMemoryScheduler(args)
    return MemoryScheduler(args)
