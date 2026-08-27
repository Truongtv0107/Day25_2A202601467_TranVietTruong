# Day 25 Reliability Engineering Final Report

## 1. Architecture summary

The gateway checks privacy-safe semantic cache entries first. Cache misses move through an ordered provider chain, with one circuit breaker per provider. An open circuit fails fast and allows a half-open probe after the reset timeout. If every provider is unavailable, the caller receives an explicit degraded response.

```text
User -> Gateway -> Cache --hit--> Cached response
                   | miss
                   v
              Primary breaker -> Primary provider
                   | fail/open
                   v
              Backup breaker  -> Backup provider
                   | fail/open
                   v
              Static degraded response
```

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Opens quickly after repeated failures while tolerating isolated errors |
| reset_timeout_seconds | 2.0 | Limits outage amplification before a recovery probe |
| success_threshold | 1 | A successful probe restores normal routing promptly |
| cache TTL | 300 s | Bounds staleness and Redis memory use |
| similarity_threshold | 0.92 | Conservative semantic reuse; year mismatches are separately rejected |
| load_test requests | 100 per scenario | Exercises repeated queries, fallback, and cache behavior |

## 3. SLO evaluation

SLOs are evaluated against the healthy scenario; recovery is evaluated across chaos scenarios.

| SLI | Target | Actual | Met? |
|---|---|---:|---|
| Availability | >= 99% | 1.0000 | Yes |
| Latency P95 | < 2500 ms | 229.7200 ms | Yes |
| Fallback success rate | >= 95% | 1.0000 | Yes |
| Cache hit rate | >= 10% | 0.6400 | Yes |
| Recovery time | < 5000 ms | 2299.0651 | Yes |

## 4. Aggregate chaos metrics

| Metric | Value |
|---|---:|
| total_requests | 500 |
| successful_requests | 397 |
| failed_requests | 103 |
| fallback_successes | 76 |
| static_fallbacks | 103 |
| cache_hits | 192 |
| availability | 0.794 |
| error_rate | 0.206 |
| latency_p50_ms | 0.38 |
| latency_p95_ms | 319.31 |
| latency_p99_ms | 511.86 |
| fallback_success_rate | 0.4246 |
| cache_hit_rate | 0.384 |
| circuit_open_count | 12 |
| recovery_time_ms | 2299.065113067627 |
| estimated_cost | 0.102528 |
| estimated_cost_saved | 0.192 |

## 5. Cache comparison

The comparison uses healthy providers and the same deterministic random seed. The cache run uses the in-memory backend so Redis overhead does not distort the cache benefit.

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---:|
| latency_p50_ms | 210.7900 | 0.3300 | -210.4600 |
| latency_p95_ms | 238.4900 | 235.3000 | -3.1900 |
| estimated_cost | 0.0593 | 0.0205 | -0.0388 |
| cache_hit_rate | 0.0000 | 0.6300 | 0.6300 |

## 6. Redis shared cache

An in-memory cache is private to one gateway process, so replicas cannot reuse each other's results and may serve different cache states. `SharedRedisCache` stores query/response hashes under a shared prefix with server-side TTLs, allowing separate gateway instances to observe the same entries. Sensitive queries are rejected before writes, and year/ID mismatches are rejected after similarity lookup.

`tests/test_redis_cache.py` covers exact lookup, TTL expiry, cross-instance visibility, privacy filtering, and false-hit rejection. The repository CI starts a Redis 7 service before running the full suite. Local execution of the 36 non-Redis test functions is saved in `reports/test_output.txt`; Redis could not be started in the restricted local sandbox.

### Shared-state evidence

```text
Test contract: tests/test_redis_cache.py::test_shared_state_across_instances
Instance c1 writes: shared query -> shared response
Instance c2 reads:  shared query -> shared response
Local execution: NOT RUN - Docker/Redis service access was denied by the sandbox
CI execution: redis:7-alpine service + redis-cli PING health check + make test
```

### Redis CLI output

```text
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
NOT CAPTURED LOCALLY - Docker service access was denied by the sandbox.
The CI workflow runs Redis-backed tests against localhost:6379 on every push/PR.
```

### In-memory vs Redis latency comparison (optional)

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 0.33 | N/A | Redis benchmark not run in the restricted sandbox |
| latency_p95_ms | 235.30 | N/A | Provider misses dominate P95 in the memory run |

## 7. Chaos scenarios

| Scenario | Expected behavior | Observed behavior | Status |
|---|---|---|---|
| primary_timeout_100 | Primary opens; backup serves requests without a retry storm | availability=1.0000, fallback_rate=1.0000, static_fallbacks=0, circuit_opens=5 | PASS |
| primary_flaky_50 | Primary failures are absorbed by the backup and circuit breaker | availability=0.9700, fallback_rate=0.8846, static_fallbacks=3, circuit_opens=3 | PASS |
| all_healthy | All requests succeed and no static fallback is used | availability=1.0000, fallback_rate=0.0000, static_fallbacks=0, circuit_opens=0 | PASS |
| all_unavailable | Both circuits open and the gateway returns the static fallback | availability=0.0000, fallback_rate=0.0000, static_fallbacks=100, circuit_opens=2 | PASS |
| primary_recovery | Primary recovers; a half-open probe closes its circuit | availability=1.0000, fallback_rate=1.0000, static_fallbacks=0, circuit_opens=2 | PASS |

## 8. Failure analysis

The remaining architectural weakness is that circuit state is local to each gateway instance. Under a large multi-instance deployment, every replica can independently probe an unhealthy provider, creating more recovery traffic than intended. Before production, breaker state and probe leases should be coordinated through Redis with atomic operations, while retaining a local fail-safe if Redis is unavailable.

## 9. Next steps

1. Add a distributed half-open probe lease and shared breaker counters.
2. Replace constant cache-cost savings with provider-specific avoided-cost accounting.
3. Add concurrent load tests and alert thresholds for latency, fallback rate, and false hits.
