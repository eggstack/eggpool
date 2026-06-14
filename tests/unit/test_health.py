"""Tests for retry classification, failover, and health management."""

from __future__ import annotations

import time

from go_aggregator.health.circuit_breaker import CircuitBreaker, CircuitState
from go_aggregator.health.health_manager import HealthManager
from go_aggregator.retry.classification import RetryCategory, RetryClassifier
from go_aggregator.retry.failover import FailoverManager


class TestRetryClassifier:
    """Tests for RetryClassifier."""

    def test_classify_400(self) -> None:
        """Test classification of 400 error."""
        classifier = RetryClassifier()
        error = classifier.classify(400)
        assert error.category == RetryCategory.BAD_REQUEST
        assert not error.is_retryable

    def test_classify_401(self) -> None:
        """Test classification of 401 error."""
        classifier = RetryClassifier()
        error = classifier.classify(401)
        assert error.category == RetryCategory.AUTH_FAILURE
        assert not error.is_retryable
        assert error.should_disable_account

    def test_classify_404(self) -> None:
        """Test classification of 404 error."""
        classifier = RetryClassifier()
        error = classifier.classify(404)
        assert error.status_code == 404
        assert error.should_remove_model

    def test_classify_429_with_retry_after(self) -> None:
        """Test classification of 429 with Retry-After."""
        classifier = RetryClassifier()
        error = classifier.classify(429, {"retry-after": "30"})
        assert error.category == RetryCategory.QUOTA_EXCEEDED
        assert error.retry_after == 30.0
        assert error.is_retryable

    def test_classify_500(self) -> None:
        """Test classification of 500 error."""
        classifier = RetryClassifier()
        error = classifier.classify(500)
        assert error.category == RetryCategory.TEMPORARY
        assert error.is_retryable

    def test_classify_503_with_retry_after(self) -> None:
        """Test classification of 503 with Retry-After."""
        classifier = RetryClassifier()
        error = classifier.classify(503, {"retry-after": "60"})
        assert error.category == RetryCategory.TEMPORARY
        assert error.retry_after == 60.0


class TestFailoverManager:
    """Tests for FailoverManager."""

    def test_should_retry_initial(self) -> None:
        """Test retry decision for initial request."""
        manager = FailoverManager()
        assert manager.should_retry("account1", "model1", 1)

    def test_should_not_retry_beyond_max(self) -> None:
        """Test retry decision beyond max retries."""
        manager = FailoverManager(max_retries=2)
        assert not manager.should_retry("account1", "model1", 3)

    def test_record_success(self) -> None:
        """Test recording successful attempt."""
        manager = FailoverManager()
        error = manager.record_attempt("account1", "model1", 200, 1, True)
        assert error.status_code == 200
        assert not manager.is_account_disabled("account1")

    def test_record_auth_failure(self) -> None:
        """Test recording auth failure disables account."""
        manager = FailoverManager(auth_disable_duration_seconds=60)
        manager.record_attempt("account1", "model1", 401, 1, False)
        assert manager.is_account_disabled("account1")

    def test_record_404_disables_model(self) -> None:
        """Test recording 404 disables model."""
        manager = FailoverManager()
        manager.record_attempt("account1", "model1", 404, 1, False)
        assert manager.is_model_disabled("account1", "model1")

    def test_circuit_breaker_opens(self) -> None:
        """Test circuit breaker opens after threshold."""
        manager = FailoverManager(circuit_breaker_threshold=3)
        for _ in range(3):
            manager.record_attempt("account1", "model1", 500, 1, False)
        assert not manager.should_retry("account1", "model1", 1)

    def test_attempt_history(self) -> None:
        """Test attempt history tracking."""
        manager = FailoverManager()
        manager.record_attempt("account1", "model1", 200, 1, True)
        manager.record_attempt("account1", "model1", 500, 2, False)

        history = manager.get_attempt_history("account1")
        assert len(history) == 2

        history = manager.get_attempt_history(model_id="model1")
        assert len(history) == 2

    def test_disable_enable_account(self) -> None:
        """Test manual disable/enable."""
        manager = FailoverManager()
        manager.disable_account("account1", "maintenance")
        assert manager.is_account_disabled("account1")

        manager.enable_account("account1")
        assert not manager.is_account_disabled("account1")


class TestCircuitBreaker:
    """Tests for CircuitBreaker."""

    def test_initial_state_closed(self) -> None:
        """Test initial state is closed."""
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request()

    def test_opens_after_threshold(self) -> None:
        """Test circuit opens after failure threshold."""
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert not cb.allow_request()

    def test_half_open_after_timeout(self) -> None:
        """Test circuit goes to half-open after timeout."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        time.sleep(0.11)
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.allow_request()

    def test_closes_after_successes(self) -> None:
        """Test circuit closes after success threshold."""
        cb = CircuitBreaker(
            failure_threshold=2, recovery_timeout=0.1, success_threshold=2
        )
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.11)
        cb.record_success()
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_records_failure_on_half_open(self) -> None:
        """Test failure on half-open reopens circuit."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.11)
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_reset(self) -> None:
        """Test circuit breaker reset."""
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request()

    def test_get_stats(self) -> None:
        """Test getting circuit breaker stats."""
        cb = CircuitBreaker()
        stats = cb.get_stats()
        assert stats["state"] == "closed"
        assert stats["failure_count"] == 0


class TestHealthManager:
    """Tests for HealthManager."""

    def test_record_success(self) -> None:
        """Test recording success."""
        manager = HealthManager()
        manager.record_success("account1", "model1")
        assert manager.is_account_healthy("account1")

    def test_record_failure(self) -> None:
        """Test recording failure."""
        manager = HealthManager()
        manager.record_failure("account1", "model1")
        health = manager.get_account_health("account1")
        assert health.consecutive_failures == 1

    def test_disable_account(self) -> None:
        """Test disabling account."""
        manager = HealthManager()
        manager.disable_account("account1", "maintenance", 60)
        assert not manager.is_account_healthy("account1")

    def test_enable_account(self) -> None:
        """Test enabling account."""
        manager = HealthManager()
        manager.disable_account("account1", "maintenance")
        manager.enable_account("account1")
        assert manager.is_account_healthy("account1")

    def test_disable_model(self) -> None:
        """Test disabling model."""
        manager = HealthManager()
        manager.disable_model("account1", "model1")
        assert not manager.is_model_healthy("account1", "model1")
        assert manager.is_model_healthy("account1", "model2")

    def test_enable_model(self) -> None:
        """Test enabling model."""
        manager = HealthManager()
        manager.disable_model("account1", "model1")
        manager.enable_model("account1", "model1")
        assert manager.is_model_healthy("account1", "model1")

    def test_get_healthy_accounts(self) -> None:
        """Test getting healthy accounts."""
        manager = HealthManager()
        manager.record_success("account1")
        manager.disable_account("account2", "maintenance")

        healthy = manager.get_healthy_accounts(["account1", "account2", "account3"])
        assert "account1" in healthy
        assert "account2" not in healthy
        assert "account3" in healthy

    def test_get_health_stats(self) -> None:
        """Test getting health stats."""
        manager = HealthManager()
        manager.record_success("account1")
        stats = manager.get_health_stats("account1")
        assert stats["account_name"] == "account1"
        assert stats["is_healthy"] is True
