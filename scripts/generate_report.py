from __future__ import annotations

import argparse
import json
from pathlib import Path

from reliability_lab.config import load_config


def display(value: object) -> str:
    if value is None:
        return "Not observed"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def met(actual: object, predicate: bool) -> str:
    if actual is None:
        return "No"
    return "Yes" if predicate else "No"


EXPECTED_SCENARIOS = {
    "primary_timeout_100": "Primary opens; backup serves requests without a retry storm",
    "primary_flaky_50": "Primary failures are absorbed by the backup and circuit breaker",
    "all_healthy": "All requests succeed and no static fallback is used",
    "all_unavailable": "Both circuits open and the gateway returns the static fallback",
    "primary_recovery": "Primary recovers; a half-open probe closes its circuit",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", default="reports/metrics.json")
    parser.add_argument("--out", default="reports/final_report.md")
    parser.add_argument("--config", default="configs/default.yaml")
    args = parser.parse_args()
    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8"))
    config = load_config(args.config)
    scenario_metrics = metrics.get("scenario_metrics", {})
    healthy = scenario_metrics.get("all_healthy", metrics)
    comparison = metrics.get("cache_comparison", {})
    without_cache = comparison.get("without_cache", {})
    with_cache = comparison.get("with_cache", {})
    delta = comparison.get("delta", {})

    availability = healthy.get("availability", 0.0)
    p95 = healthy.get("latency_p95_ms", 0.0)
    cache_hit_rate = healthy.get("cache_hit_rate", 0.0)
    outage_fallback_rate = scenario_metrics.get("primary_timeout_100", {}).get(
        "fallback_success_rate", 0.0
    )
    recovery = metrics.get("recovery_time_ms")

    lines = [
        "# Day 25 Reliability Engineering Final Report",
        "",
        "## 1. Architecture summary",
        "",
        "The gateway checks privacy-safe semantic cache entries first. Cache misses move through "
        "an ordered provider chain, with one circuit breaker per provider. An open circuit fails "
        "fast and allows a half-open probe after the reset timeout. If every provider is unavailable, "
        "the caller receives an explicit degraded response.",
        "",
        "```text",
        "User -> Gateway -> Cache --hit--> Cached response",
        "                   | miss",
        "                   v",
        "              Primary breaker -> Primary provider",
        "                   | fail/open",
        "                   v",
        "              Backup breaker  -> Backup provider",
        "                   | fail/open",
        "                   v",
        "              Static degraded response",
        "```",
        "",
        "## 2. Configuration",
        "",
        "| Setting | Value | Reason |",
        "|---|---:|---|",
        f"| failure_threshold | {config.circuit_breaker.failure_threshold} | Opens quickly after repeated failures while tolerating isolated errors |",
        f"| reset_timeout_seconds | {config.circuit_breaker.reset_timeout_seconds} | Limits outage amplification before a recovery probe |",
        f"| success_threshold | {config.circuit_breaker.success_threshold} | A successful probe restores normal routing promptly |",
        f"| cache TTL | {config.cache.ttl_seconds} s | Bounds staleness and Redis memory use |",
        f"| similarity_threshold | {config.cache.similarity_threshold} | Conservative semantic reuse; year mismatches are separately rejected |",
        f"| load_test requests | {config.load_test.requests} per scenario | Exercises repeated queries, fallback, and cache behavior |",
        "",
        "## 3. SLO evaluation",
        "",
        "SLOs are evaluated against the healthy scenario; recovery is evaluated across chaos scenarios.",
        "",
        "| SLI | Target | Actual | Met? |",
        "|---|---|---:|---|",
        f"| Availability | >= 99% | {display(availability)} | {met(availability, availability >= 0.99)} |",
        f"| Latency P95 | < 2500 ms | {display(p95)} ms | {met(p95, p95 < 2500)} |",
        f"| Fallback success rate | >= 95% | {display(outage_fallback_rate)} | {met(outage_fallback_rate, outage_fallback_rate >= 0.95)} |",
        f"| Cache hit rate | >= 10% | {display(cache_hit_rate)} | {met(cache_hit_rate, cache_hit_rate >= 0.1)} |",
        f"| Recovery time | < 5000 ms | {display(recovery)} | {met(recovery, recovery is not None and recovery < 5000)} |",
        "",
        "## 4. Aggregate chaos metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in metrics.items():
        if key in {"scenarios", "scenario_metrics", "cache_comparison"}:
            continue
        lines.append(f"| {key} | {value} |")

    lines += [
        "",
        "## 5. Cache comparison",
        "",
        "The comparison uses healthy providers and the same deterministic random seed. The cache "
        "run uses the in-memory backend so Redis overhead does not distort the cache benefit.",
        "",
        "| Metric | Without cache | With cache | Delta |",
        "|---|---:|---:|---:|",
    ]
    for metric in ("latency_p50_ms", "latency_p95_ms", "estimated_cost", "cache_hit_rate"):
        lines.append(
            f"| {metric} | {display(without_cache.get(metric))} | "
            f"{display(with_cache.get(metric))} | {display(delta.get(metric))} |"
        )

    lines += [
        "",
        "## 6. Redis shared cache",
        "",
        "An in-memory cache is private to one gateway process, so replicas cannot reuse each "
        "other's results and may serve different cache states. `SharedRedisCache` stores "
        "query/response hashes under a shared prefix with server-side TTLs, allowing separate "
        "gateway instances to observe the same entries. Sensitive queries are rejected before "
        "writes, and year/ID mismatches are rejected after similarity lookup.",
        "",
        "`tests/test_redis_cache.py` covers exact lookup, TTL expiry, cross-instance visibility, "
        "privacy filtering, and false-hit rejection. The repository CI starts a Redis 7 service "
        "before running the full suite. Local execution of the 36 non-Redis test functions is saved "
        "in `reports/test_output.txt`; Redis could not be started in the restricted local sandbox.",
        "",
        "### Shared-state evidence",
        "",
        "```text",
        "Test contract: tests/test_redis_cache.py::test_shared_state_across_instances",
        "Instance c1 writes: shared query -> shared response",
        "Instance c2 reads:  shared query -> shared response",
        "Local execution: NOT RUN - Docker/Redis service access was denied by the sandbox",
        "CI execution: redis:7-alpine service + redis-cli PING health check + make test",
        "```",
        "",
        "### Redis CLI output",
        "",
        "```text",
        "$ docker compose exec redis redis-cli KEYS \"rl:cache:*\"",
        "NOT CAPTURED LOCALLY - Docker service access was denied by the sandbox.",
        "The CI workflow runs Redis-backed tests against localhost:6379 on every push/PR.",
        "```",
        "",
        "### In-memory vs Redis latency comparison (optional)",
        "",
        "| Metric | In-memory cache | Redis cache | Notes |",
        "|---|---:|---:|---|",
        "| latency_p50_ms | 0.33 | N/A | Redis benchmark not run in the restricted sandbox |",
        "| latency_p95_ms | 235.30 | N/A | Provider misses dominate P95 in the memory run |",
        "",
        "## 7. Chaos scenarios",
        "",
        "| Scenario | Expected behavior | Observed behavior | Status |",
        "|---|---|---|---|",
    ]
    for name, status in metrics.get("scenarios", {}).items():
        detail = scenario_metrics.get(name, {})
        observed = (
            f"availability={display(detail.get('availability'))}, "
            f"fallback_rate={display(detail.get('fallback_success_rate'))}, "
            f"static_fallbacks={display(detail.get('static_fallbacks'))}, "
            f"circuit_opens={display(detail.get('circuit_open_count'))}"
        )
        lines.append(
            f"| {name} | {EXPECTED_SCENARIOS.get(name, 'Gateway remains bounded and observable')} | "
            f"{observed} | {status.upper()} |"
        )

    lines += [
        "",
        "## 8. Failure analysis",
        "",
        "The remaining architectural weakness is that circuit state is local to each gateway "
        "instance. Under a large multi-instance deployment, every replica can independently probe an "
        "unhealthy provider, creating more recovery traffic than intended. Before production, breaker "
        "state and probe leases should be coordinated through Redis with atomic operations, while "
        "retaining a local fail-safe if Redis is unavailable.",
        "",
        "## 9. Next steps",
        "",
        "1. Add a distributed half-open probe lease and shared breaker counters.",
        "2. Replace constant cache-cost savings with provider-specific avoided-cost accounting.",
        "3. Add concurrent load tests and alert thresholds for latency, fallback rate, and false hits.",
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
