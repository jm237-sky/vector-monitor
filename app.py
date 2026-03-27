"""
Vector Service Monitor — FastAPI backend (SSM edition, read-only)

Connects to EC2 instances via AWS SSM RunCommand.
AWS credentials are read automatically from environment variables:
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
  (or AWS_PROFILE, or instance role)

This application is intentionally READ-ONLY — no mutating operations.
"""
import asyncio
import json
import re
import time
from pathlib import Path
from typing import AsyncGenerator

import boto3
import yaml
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ── Config ────────────────────────────────────────────────────────────────── #

CONFIG_FILE = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


# ── SSM helpers ───────────────────────────────────────────────────────────── #

def _ssm_client(region: str):
    return boto3.client("ssm", region_name=region)


def _run_ssm_sync(instance_id: str, command: str, region: str, timeout: int = 30) -> str:
    """Blocking: send a shell command to an EC2 via AWS SSM RunCommand and return stdout."""
    ssm = _ssm_client(region)
    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            TimeoutSeconds=timeout,
        )
    except ClientError as exc:
        raise RuntimeError(f"SSM send_command failed: {exc}") from exc

    cmd_id = resp["Command"]["CommandId"]
    deadline = time.monotonic() + timeout + 15

    while time.monotonic() < deadline:
        try:
            inv = ssm.get_command_invocation(CommandId=cmd_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(1)
            continue

        status = inv["Status"]
        if status in ("Success", "Failed", "Cancelled", "TimedOut", "DeliveryTimedOut"):
            return inv.get("StandardOutputContent", "")
        time.sleep(1)

    raise TimeoutError(f"SSM command timed out after {timeout}s")


async def run_ssm(instance_id: str, command: str, region: str, timeout: int = 30) -> str:
    """Async wrapper — runs the blocking SSM call in a thread pool."""
    return await asyncio.to_thread(_run_ssm_sync, instance_id, command, region, timeout)

# ── Input validation ─────────────────────────────────────────────────────── #

# Strict allowlist: only alphanumerics, dash, underscore, dot
_SAFE_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _validate_service(service: str) -> None:
    if not _SAFE_RE.match(service):
        raise HTTPException(400, "Invalid service name")


def _unit(service: str) -> str:
    """Build the full systemd unit name from a short vector service name."""
    return f"vector-{service}.service"


def _resolve_host(env: str, host_id: str) -> tuple[dict, str]:
    config = load_config()
    env_cfg = config.get("environments", {}).get(env)
    if not env_cfg:
        raise HTTPException(404, f"Unknown environment: {env}")
    host = next((h for h in env_cfg.get("hosts", []) if h["id"] == host_id), None)
    if not host:
        raise HTTPException(404, f"Unknown host '{host_id}' in env '{env}'")
    region = config.get("aws", {}).get("region", "eu-west-1")
    return host, region


# ── App ───────────────────────────────────────────────────────────────────── #

app = FastAPI(title="Vector Monitor (read-only)", docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],   # read-only: all endpoints are GET
    allow_headers=["*"],
)

# ── API routes ────────────────────────────────────────────────────────────── #

@app.get("/api/environments")
def get_environments():
    config = load_config()
    return [
        {"id": k, "name": v.get("name", k.capitalize())}
        for k, v in config.get("environments", {}).items()
    ]


@app.get("/api/services/{env}")
async def get_services(env: str):
    """Return all vector services from all EC2 hosts in an environment."""
    config = load_config()
    env_cfg = config.get("environments", {}).get(env)
    if not env_cfg:
        raise HTTPException(404, f"Unknown environment: {env}")

    region = config.get("aws", {}).get("region", "eu-west-1")

    async def fetch_host(host: dict) -> dict:
        instance_id = host["instance_id"]
        try:
            stdout = await asyncio.wait_for(
                run_ssm(
                    instance_id,
                    "systemctl list-units --type=service --all --plain --no-legend 2>/dev/null "
                    "| grep '^vector-' || true",
                    region,
                ),
                timeout=45,
            )
            services = []
            for line in stdout.strip().splitlines():
                parts = line.split(None, 4)
                if not parts:
                    continue
                unit = parts[0]
                short = re.sub(r"^vector-", "", unit).removesuffix(".service")
                active = parts[2] if len(parts) > 2 else "unknown"
                sub    = parts[3] if len(parts) > 3 else "unknown"
                status = (
                    "running"  if active == "active"   and sub == "running" else
                    "failed"   if active == "failed"   or  sub == "failed"  else
                    "inactive" if active == "inactive" or  sub == "dead"    else
                    active
                )
                services.append({
                    "name": short,
                    "unit": unit,
                    "status": status,
                    "active": active,
                    "sub": sub,
                })
            return {
                "host_id": host["id"],
                "name": host["name"],
                "instance_id": instance_id,
                "connected": True,
                "services": sorted(services, key=lambda s: s["name"]),
            }
        except Exception as exc:
            return {
                "host_id": host["id"],
                "name": host["name"],
                "instance_id": instance_id,
                "connected": False,
                "error": str(exc),
                "services": [],
            }

    results = await asyncio.gather(*[fetch_host(h) for h in env_cfg.get("hosts", [])])
    return list(results)


@app.get("/api/logs/{env}/{host_id}/{service}")
async def get_logs(
    env: str,
    host_id: str,
    service: str,
    lines: int = 200,
    level: str = "all",
):
    _validate_service(service)
    host, region = _resolve_host(env, host_id)

    n = min(max(lines, 5), 2000)
    unit = _unit(service)

    stdout = await run_ssm(
        host["instance_id"],
        f"journalctl -u {unit} -n {n} --no-pager --output=short-iso 2>/dev/null || true",
        region,
    )

    log_lines = stdout.strip().splitlines() if stdout.strip() else []

    if level and level != "all":
        lvl = level.upper()
        log_lines = [ln for ln in log_lines if lvl in ln.upper()]

    return {
        "service": service,
        "host": host["name"],
        "lines": log_lines,
        "total": len(log_lines),
    }


@app.get("/api/status/{env}/{host_id}/{service}")
async def get_status(env: str, host_id: str, service: str):
    _validate_service(service)
    host, region = _resolve_host(env, host_id)
    unit = _unit(service)

    stdout = await run_ssm(
        host["instance_id"],
        f"systemctl status {unit} --no-pager -l 2>&1 | head -50 || true",
        region,
    )
    return {"service": service, "host": host["name"], "output": stdout}


@app.get("/api/stream/{env}/{host_id}/{service}")
async def stream_logs(env: str, host_id: str, service: str, request: Request):
    """
    SSE endpoint for near-real-time log tailing via SSM polling.
    Polls every 8 s with a 15-second look-back window to absorb SSM latency.
    """
    _validate_service(service)
    host, region = _resolve_host(env, host_id)
    instance_id = host["instance_id"]
    unit = _unit(service)

    POLL_INTERVAL = 8
    WINDOW_SECS   = 15

    async def generate() -> AsyncGenerator[str, None]:
        seen: set[str] = set()

        # Initial burst: last 50 lines
        try:
            stdout = await run_ssm(
                instance_id,
                f"journalctl -u {unit} -n 50 --no-pager --output=short-iso 2>/dev/null || true",
                region,
            )
            for line in stdout.strip().splitlines():
                seen.add(line)
                yield f"data: {json.dumps({'line': line})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
            return

        # Polling loop
        while not await request.is_disconnected():
            await asyncio.sleep(POLL_INTERVAL)
            try:
                stdout = await run_ssm(
                    instance_id,
                    f"journalctl -u {unit} --since '{WINDOW_SECS} seconds ago' "
                    "--no-pager --output=short-iso 2>/dev/null || true",
                    region,
                )
                for line in stdout.strip().splitlines():
                    if line not in seen:
                        seen.add(line)
                        yield f"data: {json.dumps({'line': line})}\n\n"
                if len(seen) > 5000:
                    seen.clear()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"
                return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Static / SPA ──────────────────────────────────────────────────────────── #

_static = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(_static)), name="static")


@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def spa(full_path: str = ""):
    return FileResponse(str(_static / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=True, log_level="info")
