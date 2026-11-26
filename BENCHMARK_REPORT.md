# Benchmark & Observability Report — Day 14

**Họ tên:** Luong Duc Anh (MSSV: 2A202603931)
**Ngày chạy:** 2026-11-26
**Môi trường:** Oracle Linux 9, aarch64, 4 vCPU / 22 GiB RAM, Podman 5.8.2 with Docker Compose 2.26.1 provider; Python 3.12 venv for local tests.

---

## 1. SLO của agent

| SLI | SLO | Lý do chọn ngưỡng này |
|---|---|---|
| p95 latency `/ask` | < 500 ms | The normal mock LLM call is about 150–220 ms; 500 ms leaves room for retrieval and runtime overhead while catching the injected slow tail. |
| Error rate `/ask` | < 2% | Keeps transient upstream failures rare; the load test uses this as its error budget. |
| Accuracy trên golden dataset | ≥ 90% | A service response is not useful if it is fast but wrong; the eval has 23 representative questions. |

---

## 2. Benchmark hiệu năng (develop, `load_test.py`)

### 2.1 Concurrency vs latency (agent v2)

| Concurrency | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Error rate | SLO |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 4.59 | 189.1 | 216.7 | 1700.9 | 1.33% | PASS |
| 10 | 36.49 | 187.1 | 224.2 | 1711.8 | 1.00% | PASS |
| 50 | 38.73 | 194.3 | 4257.6 | 6257.4 | 0.33% | FAIL (p95) |
| 100 | 29.80 | 197.7 | 8036.1 | 8291.3 | 12.33% | FAIL (p95, errors) |

**Nhận xét:** Throughput rises from concurrency 1 to 10, then plateaus and drops. The 50 and 100 user runs expose the latency and timeout tail; adding more concurrent callers does not make the serial mock LLM work finish faster.

### 2.2 Tail latency

| `MOCK_SLOW_RATE` | avg (ms) | p95 (ms) | p99 (ms) |
|---|---:|---:|---:|
| 0 | 272.3 | 222.6 | 2245.8 |
| 0.03 (mặc định) | 316.5 | 1706.1 | 2221.5 |
| 0.1 | 441.9 | 2198.6 | 3749.8 |

**Nhận xét:** p95/p99 respond more clearly to injected slow calls than the average. An average hides the small group of users waiting seconds, so the SLO uses a percentile instead.

---

## 3. Benchmark chất lượng (develop, `eval/run_eval.py`)

| Chỉ số | Agent v1 | Agent v2 | Agent v2 (sau khi sửa KB) |
|---|---:|---:|---:|
| Accuracy | 4.35% (1/23) | 91.30% (21/23) | 100% (23/23) |
| Avg tokens / câu | 56.7 | 109.0 | 116.1 |
| Tổng cost (USD) | 0.000398 | 0.000760 | 0.000812 |
| Cost / câu đúng (USD) | 0.00039795 | 0.00003619 | 0.00003530 |
| Quality gate | FAIL | PASS (≥80%) | PASS (100% target) |

**Những câu v2 trả lời sai ban đầu và cách bạn sửa:**

| ID | Keyword bị thiếu | Bạn đã sửa gì |
|---|---|---|
| `security-02` | `token`, `hết hạn`, `chữ ký` | Added a targeted knowledge-base answer describing JWT signature validation and expiry. |
| `scaling-03` | `cpu`, `memory`, `custom metrics` | Added a targeted knowledge-base answer covering HPA CPU, memory, and custom metrics. |

**Nhận xét về trade-off chất lượng / chi phí / latency giữa v1 và v2:** v2 uses roughly twice the tokens and total cost of v1, but raises accuracy from 4.35% to 91.30%; after the KB fixes it reaches 100% with only a small additional cost. Cost per correct answer falls by about 91% from v1 to fixed v2. Latency stays in the same range (v1 p50/p95 171.9/188.8 ms; initial v2 177.2/202.0 ms; fixed v2 190.0/224.3 ms). The quality gain is worth that modest latency increase. The v1-vs-v2 baseline quality-gate run also rejected v1, reporting 20 regressions.

---

## 4. Observability stack (production)

### 4.1 Screenshot

- [x] Dashboard Grafana khi đang có tải — `screenshots/grafana-dashboard.png`
- [x] Một trace chậm trong Jaeger — `screenshots/jaeger-slow-trace.png`
- [x] Một trace lỗi trong Jaeger, có event `exception` — `screenshots/jaeger-error-trace.png`
- [x] Trang Alerts của Prometheus với hai alerts đang **firing** — `screenshots/prometheus-alert-firing.png`

### 4.2 Thí nghiệm "phá hệ thống"

| # | Thay đổi | Alert nào firing | Thời gian từ lúc đổi → firing | Panel nào phản ánh rõ nhất |
|---|---|---|---|---|
| A | `MOCK_ERROR_RATE=0.2` (in combined A/B run) | `AgentHighErrorRate` | ~1 min (`for: 1m`) | Error rate / request status |
| B | `MOCK_SLOW_RATE=0.3` (in combined A/B run) | `AgentHighLatencyP95` | ~2 min (`for: 2m`) | p95 latency |
| C | `LLM_MODEL=mock-llm-large` | `LLMCostBurnRateHigh` | ~3 min from model switch (~2 min after the rate crossed threshold) | Cost burn rate (USD/hour) |
| D | Pause one agent instance | `AgentInstanceDown` | ~56 sec observed (rule hold: 30 sec) | Instances up / target health |
| E | `AGENT_VERSION=v1` | No infrastructure alert; the earlier cost alert lingered from C's 5-minute rate window | None during ~1 min of v1 traffic | Agent version / retrieval panels; golden-set eval |

Both A and B were active together under a 30-user Locust run; Prometheus showed both alerts `firing` at the same time. The rules were active from 2026-11-26 15:26:47 UTC; their configured hold times are 1 and 2 minutes respectively. For C, Prometheus first evaluated the burn rate above $2/hour at 15:47:07 UTC and showed it firing by 15:49:26 UTC. For D, the instance was paused at 15:47:28 UTC and its alert was firing by 15:48:24 UTC. In E, all three v1 instances were healthy and no latency, error, or availability alert fired under normal fault settings; the cost alert still firing was left over from C's rolling window.

**Thí nghiệm E không làm alert nào firing. Bạn sẽ phát hiện loại sự cố này trong production bằng cách nào?** Track answer quality with a scheduled golden-set evaluation and alert on accuracy/regressions. Infrastructure signals only show that the service is responsive; they do not establish that its answers are correct.

### 4.3 Debug bằng trace

Chọn 1 trace chậm nhất bạn tìm được:

- `trace_id`: `f70cd50c532a60a7a8b785b1fa48a8aa`
- Tổng thời gian: 1689.9 ms (request log)
- Span chiếm nhiều thời gian nhất: `chat mock-llm-small` (`llm_ms=1667.1`, about 98.6% of request time)
- Dòng log tương ứng (grep theo `trace_id`):

```json
{"ts":"2026-11-26T15:32:20.825576+00:00","level":"INFO","service":"observable-agent","instance":"6ff1154efcfe","event":"llm_call","trace_id":"f70cd50c532a60a7a8b785b1fa48a8aa","span_id":"874d36b03039398c","request_id":"bcaec2a8d264","model":"mock-llm-small","agent_version":"v2","input_tokens":83,"output_tokens":44,"cost_usd":3.9e-05,"llm_ms":1667.1}
{"ts":"2026-11-26T15:32:20.826037+00:00","level":"INFO","service":"observable-agent","instance":"6ff1154efcfe","event":"http_request","trace_id":"f70cd50c532a60a7a8b785b1fa48a8aa","span_id":"874d36b03039398c","request_id":"bcaec2a8d264","method":"POST","route":"/ask","status":200,"ms":1689.9}
```

Error trace log observed during the same injected-error run (the Jaeger span records an `exception` event):

```json
{"ts":"2026-11-26T15:32:21.201438+00:00","level":"INFO","service":"observable-agent","instance":"615ec4e2fdb0","event":"llm_error","trace_id":"4ae0b78a3fd89307f14d74ad5d07aa62","span_id":"1b5da9d448ad2947","request_id":"f9a6c7cfa3fb","error":"Upstream LLM error: 503 Service Unavailable (mock)"}
```

---

## 5. Benchmark scaling (production, Locust headless, 100 users, 2 phút)

| Số instance | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Failures |
|---:|---:|---:|---:|---:|---:|
| 1 | 66.07 | 190 | 220 | 1700 | 65/7877 (0.82%) |
| 3 | 66.25 | 190 | 220 | 1700 | 83/7893 (1.05%) |

**Giải thích kết quả:** Three instances did not improve p95 or throughput in this run. The mock LLM's fixed request latency dominates; adding replicas cannot reduce the time spent waiting on that dependency. This benchmark is evidence about the supplied mock workload, not a general claim that replicas never increase capacity.

---

## 6. Kết luận

- 3 điều bạn học được về benchmark & observability:
  1. p95/p99 expose slow requests that an average can hide.
  2. More concurrency or replicas do not guarantee higher throughput; identify the current bottleneck first.
  3. Metrics identify a symptom, traces locate the slow/error span, and trace IDs connect traces to structured logs; a separate eval catches answer-quality failures.
- If deploying this agent to production, add quality-regression and SLO burn-rate alerts, plus externally measured load-balancer errors and dependency saturation metrics.
