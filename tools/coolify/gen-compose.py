#!/usr/bin/env python3
"""Regenerate deploy/coolify/docker-compose.yml with inlined config/envoy.

Coolify's Docker Compose Build Pack does not reliably place repo files
into the deploy workdir for bind mounts, so we inline both files via
Docker's native `configs: { content: ... }` mechanism. This script is the
single source of truth for keeping the inlined content in sync with the
human-editable source files.

Workflow:

1. Re-renders `gateway/envoy.yaml` from `config.yaml` via the vllm-sr CLI.
2. Strips the CLI's stdout log preamble that leaks into the file.
3. Rewrites the Envoy access-log path from `/var/log/envoy_access.log`
   (not writable by the non-root envoyproxy/envoy container) to
   `/dev/stdout` so request logs surface in Coolify's container view.
4. Inlines both files into `docker-compose.yml` as Docker `configs:`
   with `content: |` blocks.

Run from the repo root:

    python3 tools/coolify/gen-compose.py

Requires `.venv-coolify/bin/vllm-sr` to be installed (see the README).
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys


# Router startup probe. The literal `$` characters are written so that
# Docker Compose's variable interpolation (which collapses `$$` to `$`)
# leaves a valid shell script behind. See:
# https://docs.docker.com/compose/compose-file/12-interpolation/
#
# After Compose interpolation, the script the container actually runs is:
#
#     set -u
#     if [ -z "${GEMINI_API_KEY:-}" ]; then
#         echo "[FATAL] ..."
#         exit 1
#     fi
#     code=$(curl -sS ... )
#     case "$code" in
#         200) ... ;;
#         401|403) ... ;;
#         404) ... ;;
#     esac
#     exec /app/start-router.sh /app/config.yaml
#
# The probe makes the single most common Coolify failure mode (key not
# propagated to the runtime, or key present but invalid for the
# configured model) fail loudly at boot with a precise message, instead
# of silently letting Gemini reject every chat request with the opaque
# "API error: 404 -" the dashboard surfaces today.
ROUTER_ENTRYPOINT_SCRIPT = r"""set -u
if [ -z "$${GEMINI_API_KEY:-}" ]; then
  echo "[FATAL] GEMINI_API_KEY is NOT set inside the router container."
  echo "        Set it under Coolify -> Environment Variables for this"
  echo "        application (mark it as Build + Runtime), then Redeploy."
  exit 1
fi
echo "[boot] GEMINI_API_KEY is present (length=$${#GEMINI_API_KEY})"
code=$$(curl -sS -o /tmp/gemini_probe.json -w '%{http_code}' --max-time 10 -H "Authorization: Bearer $${GEMINI_API_KEY}" -H "Content-Type: application/json" -d '{"model":"gemini-3.1-flash-lite","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions" || echo "probe-failed")
case "$$code" in
  200)
    echo "[boot] Gemini probe OK (HTTP 200) - key + model are valid."
    ;;
  401|403)
    echo "[FATAL] Gemini rejected the API key (HTTP $$code):"
    head -c 800 /tmp/gemini_probe.json; echo
    echo "        Verify GEMINI_API_KEY in Coolify env vars."
    exit 1
    ;;
  404)
    echo "[FATAL] Gemini returned 404 for model gemini-3.1-flash-lite:"
    head -c 800 /tmp/gemini_probe.json; echo
    echo "        Either the model is not accessible from this key/region,"
    echo "        or the provider_model_id in config.yaml is wrong."
    exit 1
    ;;
  *)
    echo "[warn] Gemini probe returned HTTP $$code - continuing, but expect failures:"
    head -c 800 /tmp/gemini_probe.json; echo
    ;;
esac
exec /app/start-router.sh /app/config.yaml
"""


def indent_block(text: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + line if line else line for line in text.splitlines())


def regenerate_envoy_yaml(repo_root: pathlib.Path, coolify_dir: pathlib.Path) -> None:
    cli = repo_root / ".venv-coolify" / "bin" / "vllm-sr"
    if not cli.exists():
        print(
            f"warning: {cli} not found - skipping envoy.yaml regeneration. "
            "Install with: python -m venv .venv-coolify && "
            ".venv-coolify/bin/pip install -e src/vllm-sr/",
            file=sys.stderr,
        )
        return

    proc = subprocess.run(
        [
            str(cli),
            "config",
            "envoy",
            "--config",
            str(coolify_dir / "config.yaml"),
        ],
        env={
            "ENVOY_EXTPROC_ADDRESS": "router",
            "ENVOY_ROUTER_API_ADDRESS": "router",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    body = "\n".join(
        line for line in proc.stdout.splitlines() if not re.match(r"^\d{4}-\d{2}-\d{2}", line)
    )
    body = body.replace("/var/log/envoy_access.log", "/dev/stdout")
    if not body.endswith("\n"):
        body += "\n"

    (coolify_dir / "gateway" / "envoy.yaml").write_text(body)
    print(f"wrote {len(body):,} bytes -> deploy/coolify/gateway/envoy.yaml")


# Static compose template. Uses `{KEY}` placeholders that we fill with
# str.format. Curly braces inside the YAML body are doubled (`{{`/`}}`)
# so str.format passes them through unchanged.
COMPOSE_TEMPLATE = """services:
  router:
    image: ghcr.io/vllm-project/semantic-router/vllm-sr:latest
    restart: unless-stopped
    # Custom entrypoint validates GEMINI_API_KEY against Gemini's
    # OpenAI-compat endpoint before starting the router so the most
    # common deploy failure (key not reaching runtime, or key invalid)
    # surfaces immediately instead of as an opaque "404 -" in the
    # dashboard Playground.
    entrypoint:
      - /bin/sh
      - -c
      - |
{router_entrypoint_indented}
    environment:
      - HF_HOME=/app/models
      - HF_TOKEN=${{HF_TOKEN:-}}
      - GEMINI_API_KEY=${{GEMINI_API_KEY:?}}
      - LOG_LEVEL=${{LOG_LEVEL:-info}}
    configs:
      - source: router_config
        target: /app/config.yaml
      - source: envoy_config
        target: /app/.vllm-sr/envoy.yaml
    volumes:
      - type: volume
        source: vsr-models
        target: /app/models
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:8080/health || curl -fsS http://localhost:9190/metrics || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 900s

  envoy:
    image: envoyproxy/envoy:v1.34-latest
    restart: unless-stopped
    depends_on:
      - router
    command:
      - "-c"
      - "/etc/envoy/envoy.yaml"
      - "--log-level"
      - "info"
    configs:
      - source: envoy_config
        target: /etc/envoy/envoy.yaml
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    ports:
      - "8899:8899"

  dashboard:
    image: ghcr.io/vllm-project/semantic-router/dashboard:latest
    restart: unless-stopped
    depends_on:
      - router
      - envoy
    environment:
      - TARGET_ROUTER_API_URL=http://router:8080
      - TARGET_ROUTER_METRICS_URL=http://router:9190/metrics
      - TARGET_ENVOY_URL=http://envoy:8899
      - TARGET_ENVOY_ADMIN_URL=http://envoy:9901
      - ENVOY_EXTPROC_ADDRESS=router
      - ENVOY_ROUTER_API_ADDRESS=router
      - ROUTER_CONFIG_PATH=/app/config/config.yaml
      - VLLM_SR_ENVOY_CONFIG_PATH=/app/.vllm-sr/envoy.yaml
      # Distinct names trigger the dashboard's "split-container" status
      # path. That path uses HTTP /health and /ready probes (via
      # TARGET_ROUTER_API_URL / TARGET_ENVOY_ADMIN_URL) instead of
      # running `supervisorctl` or `docker inspect` - neither of which
      # works inside the Coolify dashboard container. Without this,
      # the System page shows Router/Envoy as "Unknown".
      - VLLM_SR_ROUTER_CONTAINER_NAME=router
      - VLLM_SR_ENVOY_CONTAINER_NAME=envoy
      - VLLM_SR_DASHBOARD_CONTAINER_NAME=dashboard
    configs:
      - source: router_config
        target: /app/config/config.yaml
      - source: envoy_config
        target: /app/.vllm-sr/envoy.yaml
    volumes:
      - type: volume
        source: vsr-dashboard-data
        target: /app/data
    # Override entrypoint so we can fix volume ownership before the
    # image's entrypoint switches to the non-root user. Fresh named
    # volumes are root-owned by default, and the dashboard backend runs
    # as UID 65532 (nonroot) and needs to write its SQLite databases
    # under /app/data.
    entrypoint:
      - /bin/sh
      - -c
      - |
        mkdir -p /app/data /app/data/results /app/data/ml-pipeline
        chown -R 65532:65532 /app/data 2>/dev/null || true
        exec /app/entrypoint.sh /app/dashboard-backend -port=8700 -static=/app/frontend -config=/app/config/config.yaml
    ports:
      - "8700:8700"

volumes:
  vsr-models:
  vsr-dashboard-data:

configs:
  router_config:
    content: |
{config_yaml_indented}
  envoy_config:
    content: |
{envoy_yaml_indented}
"""


def main() -> None:
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    coolify_dir = repo_root / "deploy" / "coolify"
    regenerate_envoy_yaml(repo_root, coolify_dir)
    config_yaml = (coolify_dir / "config.yaml").read_text()
    envoy_yaml = (coolify_dir / "gateway" / "envoy.yaml").read_text()

    compose = COMPOSE_TEMPLATE.format(
        router_entrypoint_indented=indent_block(ROUTER_ENTRYPOINT_SCRIPT, 8),
        config_yaml_indented=indent_block(config_yaml, 6),
        envoy_yaml_indented=indent_block(envoy_yaml, 6),
    )

    out = coolify_dir / "docker-compose.yml"
    out.write_text(compose)
    print(f"wrote {len(compose):,} bytes -> {out.relative_to(repo_root)}")


if __name__ == "__main__":
    main()
