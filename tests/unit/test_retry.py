"""AOTF Retry 策略单元测试（M1）。"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from aotf.retry import (
    MAX_RETRIES,
    BASE_DELAY_S,
    BudgetExceededError,
    RateLimitError,
    RetryableError,
    ScopeViolationError,
    TemporaryNetworkError,
    retryable,
    run_with_retry,
)


def _run(coro):
    """运行 async 协程的辅助函数。"""
    return asyncio.run(coro)


class TestRetryableDecorator:
    """@retryable 装饰器行为。"""

    @retryable(max_retries=3)
    async def _flaky_func(self, should_fail: int = 0):
        if should_fail > 0:
            raise RateLimitError("too many requests")
        return "success"

    def test_success_first_attempt(self):
        result = _run(self._flaky_func(should_fail=0))
        assert result == "success"

    def test_success_after_retries(self):
        calls = []
        async def counting_func(should_fail=0):
            calls.append(1)
            if len(calls) <= should_fail:
                raise RateLimitError("retry")
            return "ok"
        result = _run(retryable(max_retries=3)(counting_func)(should_fail=2))
        assert result == "ok"
        assert len(calls) == 3

    def test_exhausted_retries_raises(self):
        async def always_fail():
            raise RateLimitError("persistent")
        with pytest.raises(RateLimitError, match="persistent"):
            _run(retryable(max_retries=2)(always_fail)())

    def test_non_retryable_error_not_caught(self):
        async def raises_budget():
            raise BudgetExceededError("over budget")
        with pytest.raises(BudgetExceededError):
            _run(retryable(max_retries=3)(raises_budget)())

    def test_scope_violation_not_caught(self):
        async def raises_scope():
            raise ScopeViolationError("out of scope")
        with pytest.raises(ScopeViolationError):
            _run(retryable(max_retries=3)(raises_scope)())


class TestRunWithRetry:
    """run_with_retry 函数式 API。"""

    def test_immediate_success(self):
        async def succeed():
            return 42
        result = _run(run_with_retry(succeed, max_retries=3))
        assert result == 42

    def test_retry_on_temporary_error(self):
        call_count = 0
        async def fail_twice():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TemporaryNetworkError("conn reset")
            return "recovered"
        result = _run(run_with_retry(fail_twice, max_retries=3))
        assert result == "recovered"
        assert call_count == 3

    def test_propagates_non_retryable(self):
        async def raises():
            raise ScopeViolationError("blocked")
        with pytest.raises(ScopeViolationError):
            _run(run_with_retry(raises, max_retries=3))


class TestErrorHierarchy:
    """错误类层次结构。"""

    def test_rate_limit_is_retryable(self):
        assert issubclass(RateLimitError, RetryableError)

    def test_network_is_retryable(self):
        assert issubclass(TemporaryNetworkError, RetryableError)

    def test_budget_not_retryable(self):
        assert not issubclass(BudgetExceededError, RetryableError)

    def test_scope_not_retryable(self):
        assert not issubclass(ScopeViolationError, RetryableError)


class TestDefaults:
    """默认常量。"""

    def test_max_retries(self):
        assert MAX_RETRIES == 3

    def test_base_delay(self):
        assert BASE_DELAY_S == 2.0
