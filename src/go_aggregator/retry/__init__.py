"""Retry and failover package for handling upstream failures."""

from __future__ import annotations

from go_aggregator.retry.classification import RetryableError, RetryClassifier
from go_aggregator.retry.failover import FailoverManager

__all__ = ["RetryClassifier", "RetryableError", "FailoverManager"]
