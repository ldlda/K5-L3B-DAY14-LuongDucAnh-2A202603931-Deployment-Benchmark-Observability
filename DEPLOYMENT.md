# Deployment Information

## Public URL

https://day14-agent-rbvx.onrender.com/

## Platform

Render Blueprint (`06-lab-complete/render.yaml`)

The Blueprint configures `ENVIRONMENT=production`, generates `AGENT_API_KEY`, connects `REDIS_URL` to the Render Key Value service, and sets `RATE_LIMIT_PER_MINUTE=10` and `DAILY_BUDGET_USD=1.0`. The API key is secret and is intentionally not recorded here.

## Public checks

Checked against the deployed URL:

| Request | Result |
| --- | ---: |
| `GET /health` | 200 |
| `GET /ready` | 200 |
| `POST /ask` without `X-API-Key` | 401 |
| `POST /ask` with `X-API-Key` | 200 |

```bash
BASE='https://day14-agent-rbvx.onrender.com'
curl -i "$BASE/health"
curl -i "$BASE/ready"
curl -i -X POST "$BASE/ask" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Hello"}'
```

With `AGENT_API_KEY` set locally to the generated Render value, check the authenticated request without putting the key in this file:

```bash
curl -i -X POST "$BASE/ask" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $AGENT_API_KEY" \
  -d '{"question":"Hello"}'
```

## Screenshots

- [Deployment dashboard](screenshots/dashboard.png)
- [Service running](screenshots/running.png)
- [Test results](screenshots/test.png)
