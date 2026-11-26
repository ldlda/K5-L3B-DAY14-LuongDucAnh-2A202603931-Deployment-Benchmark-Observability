# Day 14 Lab — Mission Answers

> Local evidence collected 2026-11-26.

## Part 1: Localhost vs Production

### Exercise 1.1: Anti-patterns found in `01-localhost-vs-production/develop/app.py`

1. The API key and database URL are hardcoded in source.
2. `DEBUG` and `MAX_TOKENS` are fixed values instead of environment configuration.
3. The app prints the question, response, and API key; this can expose secrets and user data in logs.
4. The server binds to `localhost`, uses port `8000`, and enables reload mode, which prevents a normal cloud/container setup.
5. There is no health endpoint for the platform to check.
6. There is no explicit graceful-shutdown or cleanup handling.

### Exercise 1.2: Develop run

Ran the supplied app locally. `GET /` returned **200** and `POST /ask?question=Hello` returned **200** with a mock answer.

### Exercise 1.3: Develop / production comparison

| Feature | Develop | Production | Why it matters |
|---|---|---|---|
| Configuration | Hardcoded values | `config.py` reads environment variables | The same image can use different safe settings in each environment. |
| Secrets | Key and database URL in source | Key read from `AGENT_API_KEY`; production validation rejects a missing key | Secrets can be managed outside the repository and rotated without editing code. |
| Network and port | Binds to `localhost:8000` | Binds to configured host and `PORT` | Cloud routing needs the process to listen on the injected port and container interface. |
| Health | No health route | `/health` and `/ready` | Liveness tells the platform whether to restart; readiness tells it whether to send traffic. |
| Logging | `print()`, including the key | Structured JSON logs without the key | Structured logs are easier to search, and must not leak credentials. |
| Shutdown | No in-flight request handling | Lifespan shutdown marks the app unready and lets Uvicorn drain requests | Requests are less likely to be cut off during a deploy or restart. |
| Debug mode | Reload always enabled | Reload only when `DEBUG=true` | Automatic reload is for development and adds risk in production. |

Local run evidence: both versions returned **200** for their ask endpoint; production `/health` and `/ready` each returned **200** after startup.

## Part 2: Docker

### Exercise 2.1: Dockerfile questions

1. Base image: `python:3.11`.
2. Working directory: `/app`.
3. `requirements.txt` is copied and installed before the source code so Docker can reuse the dependency layer when only application code changes.
4. `CMD` supplies the default command and can be replaced at `docker run`; `ENTRYPOINT` defines the executable and normally receives appended runtime arguments. The basic Dockerfile uses exec-form `CMD`.

### Exercise 2.2–2.3: Build and image size

Built both supplied images with the local Docker-compatible Podman CLI and measured them with `docker image inspect`:

- Develop: **1,195,530,306 bytes** (`docker image ls`: 1.2 GB).
- Production: **193,949,877 bytes** (`docker image ls`: 194 MB).
- Reduction: **83.8%** by byte size.

The develop image uses the full `python:3.11` base and one stage. The production Dockerfile installs dependencies in a builder stage, then copies only the installed packages and runtime files into `python:3.11-slim`; build tools are not in the runtime image. Both built containers returned **200** from their internal health route.

### Exercise 2.4: Compose architecture and test

```text
Client → Nginx reverse proxy → Agent
                              ├→ Redis (cache/session service)
                              └→ Qdrant (vector database)
```

Started the four-service stack. Through Nginx, `GET /health` and `POST /ask` each returned **200**. On this rootless Podman host, the local test used high host ports and a SELinux mount label in a temporary Compose copy; the repository Compose file was not changed.

## Part 3: Cloud Deployment

### Exercise 3.1 / 3.2: Public deployment

- Primary platform: **Render**.
- Public URL: https://day14-agent-rbvx.onrender.com/
- Public checks run: `/health` **200**, `/ready` **200**, unauthenticated `POST /ask` **401**, and authenticated `POST /ask` **200**. Deployment screenshots are saved under `screenshots/`.

The Render Blueprint in `06-lab-complete/render.yaml` creates the Docker web service and Redis-compatible Key Value service, generates `AGENT_API_KEY`, wires `REDIS_URL` from the Key Value service, and health-checks `/health`. The Railway config selects the Dockerfile, health-checks `/health`, and restarts on failure; Redis must be added and `REDIS_URL` configured separately.

### Exercise 3.3: Cloud Run pipeline

Cloud Build first installs dependencies and runs the test suite. It then builds the Docker image tagged with the commit SHA and `latest`, pushes the tags to Container Registry, and deploys the SHA-tagged image to managed Cloud Run. The service configuration sets scaling/resource limits, environment variables and Secret Manager references, plus liveness and startup probes.

## Part 4: API Security

### Exercise 4.1: API key

The basic app reads `X-API-Key` through FastAPI’s `APIKeyHeader` dependency. Missing key returned **401**; the configured key returned **200**; a wrong key returned **403**. Rotate it by changing the `AGENT_API_KEY` environment variable and restarting/redeploying the service.

### Exercises 4.2–4.3: JWT and rate limiting

- Login endpoint: `POST /auth/token`; demo student credentials are `student` / `demo123`.
- The server signs an HS256 JWT containing the subject and role; it expires after 60 minutes. Protected requests send `Authorization: Bearer <token>`.
- The rate limiter uses a sliding window of timestamps. A normal user gets **10 requests per 60 seconds**; the admin role gets a separate **100 requests per 60 seconds** limiter.
- Sanitized `curl -i` results (the bearer token is omitted):

  ```text
  HTTP/1.1 401 Unauthorized
  www-authenticate: Bearer

  HTTP/1.1 200 OK

  HTTP/1.1 429 Too Many Requests
  x-ratelimit-limit: 10
  x-ratelimit-remaining: 0
  retry-after: 59
  ```

- Runtime check: valid student login → **200**; ten `/ask` calls → **200**; the next call → **429**.

### Exercise 4.4: Cost guard

The Part 4 demo uses a Redis key per user and UTC month (`budget:<user_id>:<YYYY-MM>`). A Lua script atomically reserves the estimated cost before calling the mock LLM; the app settles that reservation to actual token cost, or releases it if the LLM call fails. The default is **$10/user/month** (`MONTHLY_BUDGET_USD` can override it); over-budget requests return **402**. The monthly key expires when the next month starts. A real Redis run accepted 16 concurrent $0.60 reservations under the $10 cap, then settled one reservation from $0.60 to the actual $0.00075. With a test cap of $0.0001, `/ask` returned **402** and recorded no reservation.

Rate limiting caps request frequency; the cost guard caps estimated spend. Both are needed because a small number of expensive requests can exceed a budget, while many cheap requests can still overload the service.

## Part 5: Scaling & Reliability

### Exercises 5.1–5.2: Health and graceful shutdown

- Liveness (`/health`) answers whether the process is running; readiness (`/ready`) answers whether it should receive traffic. Local run: each returned **200** after startup.
- Sent SIGTERM while a delayed `/ask` request was in flight. The request returned **200**, the process exited **0**, and the shutdown completion message was logged.

### Exercises 5.3–5.5: Stateless sessions and load balancing

The production demo stores session history in Redis with a TTL, so any agent instance can load it. If Redis is unavailable, the demo falls back to a per-process in-memory store; that fallback is explicitly not stateless and will not work across instances.

Ran the three-agent Compose stack and the supplied stateless test through Nginx. Five requests were handled by **three distinct instances**, and the session history contained all **10 messages** (five user messages and five assistant responses). The requests remained in the same session across instances because the shared Redis service stored the history. The local Podman run omitted the host port mapping and CPU resource limits in a temporary Compose copy; the repo files were left unchanged.

## Part 6: Final Project evidence

`06-lab-complete/check_production_ready.py` passed **12/12 criteria and 43/43 checks**, including the image build, health/readiness, API-key auth, Redis-backed rate limit and cost guard, environment config, JSON logs, graceful shutdown, and deployment config checks.

## AI log

- Used ChatGPT to inspect the assignment and repository, implement Part 6, and add tests. The implementation and review fixes are in commits `3395623` and `3578db9`; the full suite passed (31 tests), and the production checker passed 12/12.
- Used ChatGPT to inspect the supplied Part 1–5 examples, implement the Part 4 monthly Redis cost guard, and draft these answers. Local measurements and HTTP results above came from commands run on 2026-11-26. The public Render deployment and its URL/screenshots were completed separately; Part 7 benchmark and observability results are documented in `BENCHMARK_REPORT.md` and the submitted JSON/CSV reports.
- Used ChatGPT to run the Part 7 concurrency, slow-request, quality-evaluation, and one-versus-three-instance Locust benchmarks on 2026-11-26; the initial v2 quality misses were fixed by adding targeted KB entries. The Part 7 observability stack was run as isolated containers bound only to Tailscale ports. Error/latency injection produced `AgentHighErrorRate` and `AgentHighLatencyP95`; the large model produced `LLMCostBurnRateHigh`; pausing one replica produced `AgentInstanceDown`. Running v1 produced no infrastructure alert, while the golden-set eval showed its quality regression. Grafana, slow-trace, error-trace, and firing-alert screenshots are in `screenshots/`.
