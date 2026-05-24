"""Dedicated thread pool for long-running agent jobs (keeps FastAPI responsive)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from config import get_settings

_executor: ThreadPoolExecutor | None = None


def get_agent_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        workers = max(1, get_settings().agent_worker_threads)
        _executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="agent-worker",
        )
    return _executor


def schedule_agent_task(func: Callable[..., None], *args, **kwargs) -> None:
    """Queue a blocking agent job off the asyncio event loop."""
    get_agent_executor().submit(func, *args, **kwargs)


def shutdown_agent_executor() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=False)
        _executor = None
