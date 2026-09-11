"""AOTF Retry 策略（M1）。

错误分类 + 指数退避重试，区分可重试/不可重试错误。

设计裁决：
- 可重试：RateLimitError、TemporaryNetworkError、TimeoutError；
- 不可重试：BudgetExceededError、ScopeViolationError、ControllerMutationError；
- 退避：BASE_DELAY * 2^attempt，上限 RETRY_MAX_DELAY；
- 装饰器：@retryable 自动包装 async 函数。

边界：本模块只定义错误类和重试逻辑，不耦合 engine/state。
"""

from __future__ import annotations

import asyncio
import functools
from typing import TypeVar

__all__ = [
    "RetryableError",
    "RateLimitError",
    "TemporaryNetworkError",
    "BudgetExceededError",
    "ScopeViolationError",
    "retryable",
]

T = TypeVar("T")

# 重试配置
MAX_RETRIES = 3
BASE_DELAY_S = 2.0
RETRY_MAX_DELAY_S = 30.0


# ─── 错误类 ─────────────────────────────────────────────────────────────────

class RetryableError(Exception):
    """可重试错误基类。"""


class RateLimitError(RetryableError):
    """API 限流（429 / rate limit）。"""


class TemporaryNetworkError(RetryableError):
    """网络临时故障。"""


class BudgetExceededError(Exception):
    """预算超限（不可重试）。"""


class ScopeViolationError(Exception):
    """越界修改（不可重试）。"""


# ─── 重试装饰器 ──────────────────────────────────────────────────────────────

def retryable(max_retries: int = MAX_RETRIES) -> callable:
    """装饰 async 函数，自动重试可重试错误。

    用法：
        @retryable(max_retries=3)
        async def run_agent(role, inputs):
            ...
    """
    def decorator(func: AsyncFunction[T, T]) -> AsyncFunction[T, T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except (RateLimitError, TemporaryNetworkError) as exc:
                    last_exc = exc
                    if attempt < max_retries - 1:
                        delay = min(BASE_DELAY_S * (2 ** attempt), RETRY_MAX_DELAY_S)
                        await asyncio.sleep(delay)
                        continue
                    raise
                except (BudgetExceededError, ScopeViolationError):
                    # 不可重试，立即抛出
                    raise
                except Exception as exc:
                    # 未知错误，记录但不重试（ caller 处理）
                    import logging
                    logging.warning(f"Unexpected error in {func.__name__}: {exc}")
                    raise
            # 所有重试耗尽
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator


async def run_with_retry(
    coro_func,
    *args,
    max_retries: int = MAX_RETRIES,
    **kwargs,
) -> Any:
    """直接调用式重试（非装饰器场景）。"""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return await coro_func(*args, **kwargs)
        except (RateLimitError, TemporaryNetworkError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                delay = min(BASE_DELAY_S * (2 ** attempt), RETRY_MAX_DELAY_S)
                await asyncio.sleep(delay)
                continue
            raise
        except (BudgetExceededError, ScopeViolationError):
            raise
        except Exception as exc:
            import logging
            logging.warning(f"Unexpected error: {exc}")
            raise
    raise last_exc  # type: ignore[misc]
