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
# 优先用环境变量 HERMES_HOME 指定家目录，默认 /home/ubuntu
# （容器内 root 的 Path.home() 是 /root，找不到 ~/.hermes/jobs.json）
HERMES_DIR = Path(os.environ.get("HERMES_HOME", "/home/ubuntu/.hermes"))
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
            "deliver":     j.get("deliver", ""),
        })
    return result

# ─── System Services (dynamic scan) ─────────────────────────────────────────
def check_system_services():
    """动态扫描所有 hermes-* / ha-* 前缀的 systemd user services"""
    services = {}
    try:
        r = subprocess.run(
            ["systemctl", "--user", "list-units", "--type=service", "--all",
             "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=15
        )
        prefixes = ("hermes-", "ha-", "docker")
        for line in r.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            svc = parts[0].removesuffix(".service")
            if not any(svc.startswith(p) for p in prefixes):
                continue
            # 去掉 .service 后缀
            svc_clean = svc
            # 获取Description
            desc_r = subprocess.run(
                ["systemctl", "--user", "show", svc_clean, "--property=Description"],
                capture_output=True, text=True, timeout=5
            )
            desc = desc_r.stdout.strip().split("=", 1)[1] if "=" in desc_r.stdout else svc_clean
            # 获取active状态
            state_r = subprocess.run(
                ["systemctl", "--user", "is-active", svc_clean],
                capture_output=True, text=True, timeout=5
            )
            running = state_r.stdout.strip() in ("active", "activating")
            # 获取PID（仅running的进程）
            pid = None
            if running:
                # 匹配规则：hermes-xxx -> "hermes" 进程链中有 xxx；ha-xxx -> 进程名为 ha_xxx.py
                if svc_clean.startswith("hermes-"):
                    # hermes-gateway -> 进程名包含 hermes_cli.main 或 hermes-gateway
                    pid_r = subprocess.run(
                        ["pgrep", "-f", "hermes_cli.main"],
                        capture_output=True, text=True, timeout=5
                    )
                    if pid_r.returncode != 0:
                        pid_r = subprocess.run(
                            ["pgrep", "-f", svc_clean],
                            capture_output=True, text=True, timeout=5
                        )
                elif svc_clean.startswith("ha-"):
                    # ha-ws-daemon -> 进程cmdline中有 ha_ws_daemon.py 或 hermes (因为gateway也是daemon)
                    # 优先用进程名匹配
                    pid_r = subprocess.run(
                        ["pgrep", "-f", "ha_ws_daemon"],
                        capture_output=True, text=True, timeout=5
                    )
                    if pid_r.returncode != 0:
                        pid_r = subprocess.run(
                            ["pgrep", "-f", svc_clean],
                            capture_output=True, text=True, timeout=5
                        )
                else:
                    pid_r = subprocess.run(
                        ["pgrep", "-f", svc_clean],
                        capture_output=True, text=True, timeout=5
                    )
                if pid_r.returncode == 0:
                    pid = pid_r.stdout.strip().split()[0]
            services[svc_clean] = {"desc": desc, "running": running, "pid": pid}
    except Exception as e:
        sys.stderr.write(f"[systemd scan error] {e}\n")
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
PORT = 8081

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