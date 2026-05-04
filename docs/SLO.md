# Service-Level Objectives

Public commitments about ViFi reliability + performance. Track via
Prometheus (planned), alert in Alertmanager (planned).

## Availability

| Metric | Target | Measurement window | Error budget per 30 days |
|---|---|---|---|
| `/health` 200 OK | 99.95% | 30 days rolling | 21 minutes |
| `/predict/csi` 2xx | 99.9% | 30 days rolling | 43 minutes |
| `/predict/capture` 2xx | 99.5% | 30 days rolling | 3.6 hours |
| `/api/v1/stream` connection success | 99.0% | 30 days rolling | 7.2 hours |

`/predict/capture` has a looser SLO because it's a heavyweight
endpoint (multi-second responses); some 504 timeouts are expected on
very long captures.

## Latency

| Endpoint | p50 | p95 | p99 | Measurement |
|---|---|---|---|---|
| `/health` | 10 ms | 50 ms | 200 ms | excludes model load |
| `/predict` | 80 ms | 200 ms | 500 ms | per-window inference |
| `/predict/csi` | 100 ms | 250 ms | 600 ms | |
| `/predict/capture` | 1 s / 60 s of capture | 3 s | 10 s | dominated by parsing |
| `/api/v1/stream` round-trip | 50 ms | 150 ms | 300 ms | |

## Audit log

| Metric | Target |
|---|---|
| Audit gap (max delay between event ts_ms and audit write) | < 60 s |
| Audit log loss (records produced but never persisted) | 0 |
| Audit chain integrity | 100% (any mismatch is P1 incident) |
| Retention | 6 years (2200 days) |

## Severity definitions

| Sev | Definition | Response time |
|---|---|---|
| P1 | Service down OR data integrity compromised (audit chain mismatch, encryption failure) | <15 min |
| P2 | Degraded service (>5% error rate, slow latency) but functional | <1 h business |
| P3 | Cosmetic, single-component issue with workaround | Next business day |

## Monitoring + alerting (planned)

- Prometheus: scrape `/metrics` (planned, ROADMAP I132).
- Alertmanager: routes P1 to PagerDuty (vendor TBD); P2 to email.
- Grafana dashboard mirrors this SLO sheet.

## Error budget policy

When the 30-day error budget for any availability target is depleted:

1. All non-critical feature work halts on `main`.
2. The team writes an "error budget exhausted" CHANGELOG entry.
3. Reliability work (postmortem fixes, monitoring improvements) is
   prioritized until the budget recovers.

## Reporting

Monthly SLO review attached to a tagged release; populated from
Grafana exports.
