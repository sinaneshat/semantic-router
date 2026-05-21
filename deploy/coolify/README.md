# Coolify Deployment

Self-host vLLM Semantic Router on a Coolify-managed server using the bundled
Docker Compose stack. This directory contains everything Coolify needs:

```
deploy/coolify/
├── docker-compose.yml      # Router + Envoy + Dashboard (self-contained)
├── config.yaml             # Routing config source-of-truth (mirror of inlined content)
├── gateway/
│   └── envoy.yaml          # Pre-rendered Envoy config (mirror of inlined content)
└── README.md
```

> Both `config.yaml` and `gateway/envoy.yaml` are **inlined inside `docker-compose.yml`**
> as Docker `configs:` with `content:` blocks. This avoids Coolify's bind-mount
> path-resolution issues. The standalone files are kept as the human-editable
> source of truth — when you change them, regenerate the compose file (see
> "Updating the configuration" below) and commit all three.

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
> `start_period: 900s`.
>
> **First-deploy caveat:** the router's classifier auto-discovery runs once at
> startup, before the model download finishes. After the first successful
> deploy you'll see a one-time `No classifier initialized. Using placeholder
> service.` warning. **Restart the router service once from the Coolify UI**
> after models finish downloading — subsequent restarts pick up the cached
> models from the `vsr-models` volume and initialize the classifier normally.

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

Edit `config.yaml`, then run a single regen command:

```bash
# From the repo root (one-time venv setup)
python -m venv .venv-coolify
.venv-coolify/bin/pip install -e src/vllm-sr/

# Regenerate gateway/envoy.yaml and re-inline both files into docker-compose.yml
python3 tools/coolify/gen-compose.py

# (Optional) Validate the result
.venv-coolify/bin/vllm-sr validate --config deploy/coolify/config.yaml
GEMINI_API_KEY=test docker compose -f deploy/coolify/docker-compose.yml config --quiet
```

The script:

1. Re-renders `gateway/envoy.yaml` from `config.yaml` using `vllm-sr config envoy`.
2. Strips the CLI's log preamble and rewrites the access-log path to `/dev/stdout`
   (the non-root `envoyproxy/envoy` image cannot write to `/var/log/...`).
3. Inlines both files into `docker-compose.yml` via Docker `configs:`.

Commit `config.yaml`, `gateway/envoy.yaml`, and `docker-compose.yml`, push,
then **Redeploy** in Coolify.

## Recommended Hetzner server

For **thousands of requests per hour** with consistent latency, a
**CCX23 Dedicated** instance (4 vCPU AMD Milan, 16 GB RAM, 160 GB disk) is the
default sweet spot. See the top-level deployment discussion for sizing details.

## Troubleshooting

| Symptom                                                       | Fix                                                                                                                                                                                                                              |
|---------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Router unhealthy for first 5–10 min                           | Expected — HuggingFace model download (~1.5 GB). Watch `docker logs <router>`.                                                                                                                                                  |
| `No classifier initialized` after first deploy                | One-time race during model auto-discovery. Usually self-corrects within ~30 s; if not, restart `router` in Coolify UI.                                                                                                          |
| 502 from API domain                                           | Verify `envoy` is healthy and the public domain forwards to `:8899`.                                                                                                                                                            |
| Dashboard Playground returns `404`                            | Almost always an invalid `provider_model_id` in `config.yaml`. The Gemini API at `generativelanguage.googleapis.com` accepts bare names (`gemini-3.1-flash-lite`, `gemini-3.5-flash`) — **not** `google/...` (Vertex AI only).  |
| Dashboard System page shows Router/Envoy `Unknown`            | The dashboard needs the `VLLM_SR_*_CONTAINER_NAME` env vars set to distinct values so it uses HTTP probes instead of `supervisorctl`. These are already in the generated compose.                                              |
| Dashboard can't reach router                                  | Confirm all `TARGET_*` env vars use service names (`router`, `envoy`).                                                                                                                                                          |
| `GEMINI_API_KEY` errors at startup                            | Set the secret in Coolify and redeploy.                                                                                                                                                                                         |
| Config change not picked up                                   | Re-run `python3 tools/coolify/gen-compose.py`, commit all three files, redeploy.                                                                                                                                                |
| `is a directory` / `not a directory` mount errs               | Compose inlines configs — should be impossible. If it recurs, run the regen script and redeploy.                                                                                                                                |
