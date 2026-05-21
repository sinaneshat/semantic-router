# Coolify Deployment

Self-host vLLM Semantic Router on a Coolify-managed server using the bundled
Docker Compose stack. This directory contains everything Coolify needs:

```
deploy/coolify/
├── docker-compose.yml      # Router + Envoy + Dashboard
├── config.yaml             # Routing config (Gemini backends)
├── envoy/
│   └── envoy.yaml          # Pre-rendered Envoy config (do not edit by hand)
└── README.md
```

## What is deployed

| Service     | Image                                                           | Ports          |
|-------------|------------------------------------------------------------------|----------------|
| `router`    | `ghcr.io/vllm-project/semantic-router/vllm-sr:latest`            | internal       |
| `envoy`     | `envoyproxy/envoy:v1.34-latest`                                  | `8899` (API)   |
| `dashboard` | `ghcr.io/vllm-project/semantic-router/dashboard:latest`          | `8700` (UI)    |

LLM inference is delegated to the **Gemini API** — no GPU required on the
Coolify host.

## Required environment variables

| Variable          | Required | Notes                                          |
|-------------------|----------|------------------------------------------------|
| `GEMINI_API_KEY`  | ✅       | Used by the router to call Gemini             |
| `HF_TOKEN`        | optional | Only for gated HuggingFace classifier models  |
| `LOG_LEVEL`       | optional | `debug`, `info` (default), `warn`, `error`    |

The compose file marks `GEMINI_API_KEY` as required (`${GEMINI_API_KEY:?}`),
so Coolify will refuse to deploy until it is set.

## Deploy via Coolify (recommended)

1. **Push this directory to a Git repo** Coolify can read (GitHub, GitLab, etc.).
2. In Coolify: **Project → Add Resource → Application → Public/Private Repository**.
3. Pick the repo and branch.
4. Set:
   - **Build Pack**: `Docker Compose`
   - **Base Directory**: `/deploy/coolify`
   - **Docker Compose location**: `docker-compose.yml`
5. **Environment Variables** tab — set `GEMINI_API_KEY` (mark it as secret).
6. **Domains** — after first deploy:
   - Assign your API domain to the `envoy` service, e.g. `https://api.yourdomain.com:8899`
   - Assign your dashboard domain to the `dashboard` service, e.g. `https://dashboard.yourdomain.com:8700`
7. Click **Deploy**.

> First start downloads ~1.5 GB of classifier models — allow 5–10 minutes for
> the router to become healthy. Coolify's healthcheck respects this via
> `start_period: 600s`.

## Deploy via Service Stack (no Git)

1. Coolify: **Project → Add Resource → Service → Docker Compose**.
2. Paste the contents of `docker-compose.yml`.
3. Add `config.yaml` and `.vllm-sr/envoy.yaml` as bind-mounted files using
   Coolify's `content:` annotation, or upload them via SSH to the project
   working directory.
4. Set env vars and deploy as above.

## Verify

```bash
# OpenAI-compatible API (Envoy)
curl https://api.yourdomain.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "MoM",
    "messages": [{"role": "user", "content": "Write a Python function that reverses a string."}]
  }'

# Dashboard
open https://dashboard.yourdomain.com
```

The router classifies the incoming prompt and chooses between
`google/gemini-3.1-flash-lite` (default) and `google/gemini-3.5-flash`
(coding/copywriting) based on the rules in `config.yaml`.

## Updating the configuration

Edit `config.yaml`, then regenerate the Envoy config locally:

```bash
# From the repo root
python -m venv .venv-coolify
.venv-coolify/bin/pip install -e src/vllm-sr/

ENVOY_EXTPROC_ADDRESS=router \
ENVOY_ROUTER_API_ADDRESS=router \
.venv-coolify/bin/vllm-sr config envoy \
  --config deploy/coolify/config.yaml \
  > deploy/coolify/envoy/envoy.yaml

.venv-coolify/bin/vllm-sr validate --config deploy/coolify/config.yaml
```

Commit, push, then **Redeploy** in Coolify.

## Recommended Hetzner server

For **thousands of requests per hour** with consistent latency, a
**CCX23 Dedicated** instance (4 vCPU AMD Milan, 16 GB RAM, 160 GB disk) is the
default sweet spot. See the top-level deployment discussion for sizing details.

## Troubleshooting

| Symptom                              | Fix                                                       |
|--------------------------------------|-----------------------------------------------------------|
| Router unhealthy for first 5–10 min  | Expected — model download. Watch `docker logs <router>`. |
| 502 from API domain                  | Verify `envoy` is healthy and domain uses port `:8899`.  |
| Dashboard can't reach router         | Confirm all `TARGET_*` env vars use service names.       |
| `GEMINI_API_KEY` errors at startup   | Set the secret in Coolify and redeploy.                  |
| Config change not picked up          | Regenerate `envoy.yaml` and redeploy.                    |
