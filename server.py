#!/usr/bin/env python3
"""
Hermes Jobs Monitor - Backend API Server
Serves cron job status, systemd services, and daemon info as JSON.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import http.server
import socketserver
from urllib.parse import urlparse, parse_qs

# ─── Paths ────────────────────────────────────────────────────────────────────
HERMES_DIR = Path.home() / ".hermes"
CRON_JOBS_FILE = HERMES_DIR / "cron" / "jobs.json"
DAEMON_SCRIPT = HERMES_DIR / "scripts" / "ha_ws_daemon.py"

# ─── Cron Jobs ────────────────────────────────────────────────────────────────
def load_cron_jobs():
    if not CRON_JOBS_FILE.exists():
        return []
    with open(CRON_JOBS_FILE) as f:
        data = json.load(f)
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    result = []
    for j in jobs:
        result.append({
            "name":        j.get("name", "?"),
            "schedule":    j.get("schedule"),
            "last_run_at": j.get("last_run_at"),
            "next_run_at": j.get("next_run_at"),
            "last_status": j.get("last_status"),
            "last_delivery_error": j.get("last_delivery_error"),
            "enabled":     j.get("enabled", True),
            "state":       j.get("state", "?"),
            "script":      j.get("script", ""),
            "no_agent":    j.get("no_agent", False),
            "job_id":      j.get("job_id", ""),
        })
    return result

# ─── System Services ──────────────────────────────────────────────────────────
SYSTEM_SERVICES = [
    ("ha-ws-daemon",  "HA WebSocket 守护进程"),
    ("docker",        "Docker 容器引擎"),
]

def check_system_services():
    services = {}
    for svc, desc in SYSTEM_SERVICES:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", svc],
                capture_output=True, text=True, timeout=10
            )
            running = r.stdout.strip() == "active"
            services[svc] = {"desc": desc, "running": running, "pid": None}
            # Try to get PID
            if running and svc == "ha-ws-daemon":
                r2 = subprocess.run(
                    ["pgrep", "-f", "ha_ws_daemon.py"],
                    capture_output=True, text=True, timeout=5
                )
                if r2.returncode == 0:
                    pids = r2.stdout.strip().split()
                    services[svc]["pid"] = pids[0] if pids else None
        except Exception:
            services[svc] = {"desc": desc, "running": False, "pid": None}
    return services

# ─── Docker Containers ────────────────────────────────────────────────────────
def check_docker_containers():
    containers = {}
    try:
        r = subprocess.run(
            ["sudo", "docker", "ps", "--format", "{{.Names}} {{.Status}}"],
            capture_output=True, text=True, timeout=15
        )
        for line in r.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(" ", 1)
            name = parts[0]
            status = parts[1] if len(parts) > 1 else ""
            containers[name] = {
                "desc":   name,
                "running": "Up" in status,
                "status": status,
            }
    except Exception:
        pass
    return containers

# ─── Main system info ─────────────────────────────────────────────────────────
def get_system_info():
    svcs = check_system_services()
    dockers = check_docker_containers()
    return {**svcs, **dockers}

# ─── HTTP Server ──────────────────────────────────────────────────────────────
PORT = 8899

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path

        if path == "/api/jobs":
            self.send_json(load_cron_jobs())
        elif path == "/api/system":
            self.send_json(get_system_info())
        elif path == "/api/all":
            self.send_json({
                "jobs":   load_cron_jobs(),
                "system": get_system_info(),
            })
        else:
            super().do_GET()

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False, indent=2)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # silence access logs

if __name__ == "__main__":
    print(f"[Hermes Jobs Monitor] 启动于 http://0.0.0.0:{PORT}")
    print(f"  - Cron jobs API:  http://0.0.0.0:{PORT}/api/jobs")
    print(f"  - System API:     http://0.0.0.0:{PORT}/api/system")
    print(f"  - Dashboard:      http://0.0.0.0:{PORT}/")
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()