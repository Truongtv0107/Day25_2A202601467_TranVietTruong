from __future__ import annotations

import json
import random
import time
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    Recovery time calculation:
    1. For each breaker in gateway.breakers.values():
       - Walk breaker.transition_log entries
       - Track when circuit goes to "open" (save ts)
       - Track when circuit goes to "closed" (compute delta from open ts)
       - Recovery time = (close_ts - open_ts) * 1000 (convert to ms)
    2. Return average of all recovery times, or None if no recovery occurred.

    Each transition_log entry is a dict with keys: "from", "to", "reason", "ts"
    where "ts" is time.time() (epoch seconds).
    """
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        opened_at: float | None = None
        for transition in breaker.transition_log:
            destination = transition["to"]
            timestamp = float(transition["ts"])
            if destination == "open":
                opened_at = timestamp
            elif destination == "closed" and opened_at is not None:
                recovery_times.append(max(0.0, (timestamp - opened_at) * 1000))
                opened_at = None

    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario.

    Scenario runner behavior:
    1. Build gateway with build_gateway(config, scenario.provider_overrides or None)
    2. Create empty RunMetrics()
    3. Loop config.load_test.requests times:
       a. Pick random query from queries
       b. Call gateway.complete(prompt)
       c. Update metrics:
          - total_requests += 1
          - estimated_cost += result.estimated_cost
          - If cache_hit: cache_hits += 1, estimated_cost_saved += 0.001
          - If route == "fallback": fallback_successes += 1, successful_requests += 1
          - If route == "static_fallback": static_fallbacks += 1, failed_requests += 1
          - Else: successful_requests += 1
          - If result.latency_ms > 0: append to latencies_ms
    4. Count circuit_open_count from breaker transition logs (entries where to == "open")
    5. Set recovery_time_ms via calculate_recovery_time_ms(gateway)
    6. Return metrics
    """
    if not queries:
        raise ValueError("At least one query is required to run a scenario")

    scenario_config = config
    if scenario.cache_enabled is not None:
        cache_config = config.cache.model_copy(update={"enabled": scenario.cache_enabled})
        scenario_config = config.model_copy(update={"cache": cache_config})
    gateway = build_gateway(scenario_config, scenario.provider_overrides or None)
    metrics = RunMetrics()

    for request_index in range(config.load_test.requests):
        if (
            scenario.recover_provider is not None
            and scenario.recover_after_requests is not None
            and request_index == scenario.recover_after_requests
        ):
            matching_providers = [
                provider for provider in gateway.providers if provider.name == scenario.recover_provider
            ]
            if not matching_providers:
                raise ValueError(f"Unknown recovery provider: {scenario.recover_provider}")
            matching_providers[0].fail_rate = 0.0

        request_started = time.perf_counter()
        result = gateway.complete(random.choice(queries))
        request_latency_ms = (time.perf_counter() - request_started) * 1000
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost

        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001

        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1

        metrics.latencies_ms.append(request_latency_ms)

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for transition in breaker.transition_log
        if transition["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.close()
    return metrics


def _metrics_summary(metrics: RunMetrics) -> dict[str, object]:
    report = metrics.to_report_dict()
    report.pop("scenarios")
    report.pop("scenario_metrics")
    report.pop("cache_comparison")
    return report


def _scenario_passed(name: str, metrics: RunMetrics) -> bool:
    if name == "primary_timeout_100":
        return metrics.fallback_success_rate >= 0.9 and metrics.circuit_open_count >= 1
    if name == "primary_flaky_50":
        return metrics.availability >= 0.95 and metrics.fallback_successes >= 1
    if name == "all_healthy":
        return metrics.availability == 1.0 and metrics.static_fallbacks == 0
    if name == "all_unavailable":
        return metrics.static_fallbacks == metrics.total_requests
    if name == "primary_recovery":
        return metrics.availability == 1.0 and metrics.recovery_time_ms is not None
    return metrics.successful_requests > 0


def _cache_comparison(config: LabConfig, queries: list[str]) -> dict[str, dict[str, object]]:
    healthy = ScenarioConfig(
        name="cache_comparison",
        description="Healthy providers isolate cache latency and cost effects",
        provider_overrides={provider.name: 0.0 for provider in config.providers},
    )
    memory_cache = config.cache.model_copy(update={"enabled": True, "backend": "memory"})
    with_cache_config = config.model_copy(update={"cache": memory_cache})
    no_cache = config.cache.model_copy(update={"enabled": False})
    without_cache_config = config.model_copy(update={"cache": no_cache})

    random.seed(202601467)
    without_cache = run_scenario(without_cache_config, queries, healthy)
    random.seed(202601467)
    with_cache = run_scenario(with_cache_config, queries, healthy)

    without_summary = _metrics_summary(without_cache)
    with_summary = _metrics_summary(with_cache)
    delta = {
        "latency_p50_ms": round(with_ncache.percentile(50) - without_cache.percentile(50), 2),
        "latency_p95_ms": round(with_cache.percentile(95) - without_cache.percentile(95), 2),
        "estimated_cost": round(with_ncache.estimated_cost - without_cache.estimated_cost, 6),
        "cache_hit_rate": round(with_cache.cache_hit_rate - without_cache.cache_hit_rate, 4),
    }
    return {
        "without_cache": without_summary,
        "with_cache": with_summary,
        "delta": delta,
    }


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    Includes a deterministic cache comparison after the configured chaos scenarios.
    """
    random.seed(202601467)
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        metrics.scenario_metrics["default"] = _metrics_summary(metrics)
        metrics.cache_comparison = _cache_comparison(config, queries)
        return metrics

    combined = RunMetrics()
    recovery_times: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        passed = _scenario_passed(scenario.name, result)
        combined.scenarios[scenario.name] = "pass" if passed else "fail"
        combined.scenario_metrics[scenario.name] = _metrics_summary(result)

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            recovery_times.append(result.recovery_time_ms)

    if recovery_times:
        combined.recovery_time_ms = sum(recovery_times) / len(recovery_times)
    combined.cache_comparison = _cache_comparison(config, queries)
    return combined
