"""Tests for retry classification and health management."""

from __future__ import annotations

import time

from eggpool.health.circuit_breaker import CircuitBreaker, CircuitState
from eggpool.health.health_manager import HealthManager
from eggpool.retry.classification import RetryCategory, RetryClassifier


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
        assert not error.should_remove_model

    def test_classify_404_model_specific(self) -> None:
        """Test classification of 404 with model-specific body."""
        classifier = RetryClassifier()
        body = b'{"error": {"message": "model not found"}}'
        error = classifier.classify(404, body=body)
        assert error.status_code == 404
        assert error.category == RetryCategory.MODEL_UNAVAILABLE
        assert error.should_remove_model
        assert error.should_disable_model
        assert error.is_retryable

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
        """Test circuit goes to half-open after timeout via allow_request."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        time.sleep(0.11)
        assert cb.allow_request()
        assert cb.state == CircuitState.HALF_OPEN

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
        assert cb.allow_request()
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

    def test_half_open_allows_multiple_probes(self) -> None:
        """After a successful probe, another allow_request must be permitted."""
        cb = CircuitBreaker(
            failure_threshold=2, recovery_timeout=0.1, success_threshold=3
        )
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.11)
        # First probe
        assert cb.allow_request()
        assert cb.state == CircuitState.HALF_OPEN
        cb.record_success()
        # Second probe must be allowed (in-flight flag cleared)
        assert cb.allow_request()
        cb.record_success()
        # Third probe must be allowed
        assert cb.allow_request()
        cb.record_success()
        # Threshold reached: circuit should close
        assert cb.state == CircuitState.CLOSED

    def test_can_request_does_not_consume_half_open_slot(self) -> None:
        """can_request() must not transition OPEN→HALF_OPEN or set in-flight."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        time.sleep(0.11)
        # can_request should return True (recovery timeout elapsed)
        # but must NOT transition to HALF_OPEN or set half_open_in_flight
        assert cb.can_request()
        # State should still report HALF_OPEN via the property (lazy transition)
        # but allow_request should still be available (slot not consumed)
        assert cb.allow_request()
        # A second allow_request should fail (slot now consumed)
        assert not cb.allow_request()
        # But a second can_request should still return False (slot consumed)
        assert not cb.can_request()

    def test_can_request_closed_circuit(self) -> None:
        """can_request() returns True for a closed circuit."""
        cb = CircuitBreaker()
        assert cb.can_request()

    def test_can_request_open_circuit(self) -> None:
        """can_request() returns False for an open circuit within recovery."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=10)
        cb.record_failure()
        cb.record_failure()
        assert not cb.can_request()


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

    def test_record_failure_auth_sets_authentication_failed(self) -> None:
        """Auth failure should set health_state to authentication_failed."""
        manager = HealthManager()
        manager.record_failure("account1", reason="authentication_failed")
        health = manager.get_account_health("account1")
        assert health.health_state == "authentication_failed"
        assert not health.is_healthy

    def test_record_success_resets_health_state(self) -> None:
        """Success should reset health_state to healthy, except for auth failures."""
        manager = HealthManager()
        # Non-auth failure: success should clear it
        manager.record_failure("account1", reason="rate_limited")
        assert manager.get_account_health("account1").health_state == "rate_limited"
        manager.record_success("account1")
        assert manager.get_account_health("account1").health_state == "healthy"
        assert manager.is_account_healthy("account1")

    def test_record_success_preserves_auth_failure_state(self) -> None:
        """Success should NOT clear authentication_failed state (terminal)."""
        manager = HealthManager()
        manager.record_failure("account1", reason="authentication_failed")
        assert manager.get_account_health("account1").health_state == (
            "authentication_failed"
        )
        manager.record_success("account1")
        assert manager.get_account_health("account1").health_state == (
            "authentication_failed"
        )

    def test_disable_model_only_affects_that_model(self) -> None:
        """Disabling model A should not affect model B on same account."""
        manager = HealthManager()
        manager.disable_model("account1", "model-a")
        assert not manager.is_model_healthy("account1", "model-a")
        assert manager.is_model_healthy("account1", "model-b")

    def test_invalid_client_request_does_not_affect_health(self) -> None:
        """400 errors should not change health state."""
        manager = HealthManager()
        manager.record_success("account1")
        # Simulating a 400 error - should not be recorded as failure
        # since 400s are client errors, not upstream failures
        health = manager.get_account_health("account1")
        assert health.is_healthy
        assert health.consecutive_failures == 0
        assert health.health_state == "healthy"
