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
def describe_cron_schedule(schedule):
    """把 cron 表达式或调度配置转成人类可读的中文描述。

    支持的输入：
      - 字符串：标准 5 段 cron 表达式，如 "0 8 * * *"、"*/30 * * * *"
      - dict：{"expr": "..."} 或 {"display": "..."} 或 {"schedule": "..."}
    """
    if not schedule:
        return "—"

    # dict 形式：优先用 display，其次 expr / schedule 字段
    if isinstance(schedule, dict):
        if schedule.get("display"):
            return schedule["display"]
        expr = schedule.get("expr") or schedule.get("schedule") or ""
        if not expr:
            return "—"
        return describe_cron_expr(expr)

    if isinstance(schedule, str):
        return describe_cron_expr(schedule)

    return "—"


def describe_cron_expr(expr):
    """解析标准 5 段 cron 表达式 → 中文可读描述。

    例：
      "0 8 * * *"      → "每天 08:00"
      "30 6 * * 1-5"   → "周一至周五 06:30"
      "*/15 * * * *"   → "每 15 分钟"
      "0 */2 * * *"    → "每 2 小时"
    """
    expr = str(expr).strip()
    if not expr:
        return "—"

    parts = expr.split()
    if len(parts) != 5:
        # 非 cron 表达式，原样返回
        return expr

    minute, hour, day, month, weekday = parts

    WEEKDAYS = {"0": "周日", "1": "周一", "2": "周二", "3": "周三",
                "4": "周四", "5": "周五", "6": "周六", "7": "周日"}

    # ── 高频模式优先 ──
    # 每分钟
    if minute == "*" and hour == "*":
        return "每分钟"
    # 每 N 分钟
    if minute.startswith("*/") and hour == "*":
        return f"每 {minute[2:]} 分钟"
    # 每 N 小时（整点）
    if minute == "0" and hour.startswith("*/"):
        return f"每 {hour[2:]} 小时"

    # ── 时间描述 ──
    def fmt_time(h, m):
        try:
            return f"{int(h):02d}:{int(m):02d}"
        except (ValueError, TypeError):
            return f"{h}:{m}"

    # 解析 hour / minute 可能的列表/范围
    def expand(field, kind="time"):
        if field == "*":
            return None  # 通配，不限制
        if field.startswith("*/"):
            return f"每 {field[2:]} {kind}"
        return field

    # 周几描述
    def weekday_desc(w):
        if w == "*":
            return None
        # 范围，如 1-5
        if "-" in w:
            a, b = w.split("-", 1)
            return f"{WEEKDAYS.get(a, a)}至{WEEKDAYS.get(b, b)}"
        # 列表，如 1,3,5
        if "," in w:
            items = [WEEKDAYS.get(x.strip(), x.strip()) for x in w.split(",")]
            return "、".join(items)
        return WEEKDAYS.get(w, w)

    # 日期描述
    def day_desc(d):
        if d == "*":
            return None
        if d.startswith("*/"):
            return f"每 {d[2:]} 天"
        if "," in d:
            return f"每月 {d} 号"
        return f"每月 {d} 号"

    # ── 组装 ──
    # 固定时间点（minute/hour 都是数字或列表）
    time_desc = ""

    def expand_list(field):
        """展开 '9,18' → ['9','18']；'1-3' → ['1','2','3']；其它返回 [field]"""
        if "," in field:
            return [x.strip() for x in field.split(",")]
        if "-" in field:
            a, b = field.split("-", 1)
            try:
                return [str(i) for i in range(int(a), int(b) + 1)]
            except ValueError:
                return [field]
        return [field]

    if not minute.startswith("*/") and not hour.startswith("*/"):
        hours = expand_list(hour)
        minutes = expand_list(minute)
        # 笛卡尔积 → 多个时间点
        times = [fmt_time(h, m) for h in hours for m in minutes]
        time_desc = "、".join(times)
    elif minute.startswith("*/") and hour == "*":
        time_desc = ""  # 已在前面返回
    elif hour.startswith("*/") and minute == "0":
        time_desc = ""  # 已在前面返回
    else:
        # 混合，原样
        time_desc = f"{hour}:{minute}".replace("*", "每")

    # 日期部分
    d = day_desc(day)
    # 周几部分
    w = weekday_desc(weekday)

    # 组合：周几/日期在前，时间在后
    desc_parts = []
    if w:
        desc_parts.append(w)
    if d:
        desc_parts.append(d)
    if time_desc:
        prefix = "" if (w or d) else "每天 "
        desc_parts.append(f"{prefix}{time_desc}")

    result = " ".join(desc_parts).strip()
    return result or expr


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
            "schedule":    describe_cron_schedule(j.get("schedule")),
            "schedule_raw": j.get("schedule"),
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

# 服务名 → 中文备注映射（按前缀/关键字匹配）
SERVICE_ALIASES = {
    "hermes-gateway": "Hermes 网关",
    "hermes-agent": "Hermes 代理",
    "hermes-cli": "Hermes 命令行",
    "ha-ws-daemon": "HA WebSocket 守护进程",
    "ha-ws": "HA WebSocket 守护进程",
    "docker": "Docker 容器",
}


def alias_for(name):
    """根据服务名返回中文备注"""
    if name in SERVICE_ALIASES:
        return SERVICE_ALIASES[name]
    low = name.lower()
    for key, alias in SERVICE_ALIASES.items():
        if key in low:
            return alias
    return ""


def format_uptime(seconds):
    """把秒数格式化为可读时长，如 '3天 2小时' / '15分钟' / '45秒'"""
    try:
        seconds = int(seconds)
    except (ValueError, TypeError):
        return ""
    if seconds <= 0:
        return ""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}天 {hours}小时"
    if hours > 0:
        return f"{hours}小时 {mins}分钟"
    if mins > 0:
        return f"{mins}分钟"
    return f"{secs}秒"


def get_process_uptime(pid):
    """通过 ps 获取进程运行时长（秒）"""
    if not pid:
        return ""
    try:
        r = subprocess.run(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            return format_uptime(r.stdout.strip())
    except Exception:
        pass
    return ""


def get_systemd_uptime(svc):
    """通过 systemd 获取服务运行时长（基于 ActiveEnterTimestamp）"""
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", svc, "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5
        )
        ts_str = r.stdout.strip().split("=", 1)[1] if "=" in r.stdout else ""
        if not ts_str:
            return ""
        # 格式如: Wed 2024-01-10 08:30:00 CST
        from datetime import datetime
        # 去掉时区后缀，解析
        ts_clean = ts_str.rsplit(" ", 1)[0]  # 去掉 CST/UTC
        dt = datetime.strptime(ts_clean, "%a %Y-%m-%d %H:%M:%S")
        delta = (datetime.now() - dt).total_seconds()
        return format_uptime(delta)
    except Exception:
        return ""


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
            uptime = ""
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
                    # 优先用 pid 查进程时长，失败则用 systemd 时间戳
                    uptime = get_process_uptime(pid) or get_systemd_uptime(svc_clean)
                else:
                    uptime = get_systemd_uptime(svc_clean)
            services[svc_clean] = {
                "desc": desc,
                "alias": alias_for(svc_clean),
                "running": running,
                "pid": pid,
                "uptime": uptime,
            }
    except Exception as e:
        sys.stderr.write(f"[systemd scan error] {e}\n")
    return services

# ─── Docker Containers ────────────────────────────────────────────────────────
def parse_docker_uptime(status):
    """从 docker ps 的 Status 字段解析运行时长。

    示例：
      "Up 3 days"          -> "3天 0小时"
      "Up 2 hours"         -> "2小时 0分钟"
      "Up 45 minutes"      -> "45分钟"
      "Up 10 seconds"      -> "10秒"
      "Up About a minute"  -> "1分钟"
      "Exited (0) 2 hours ago" -> ""
    """
    if not status or "Up" not in status:
        return ""
    # 提取 Up 后面的部分
    up_part = status.split("Up", 1)[1].strip()
    # 去掉可能的括号说明，如 "Up 2 hours (healthy)"
    up_part = up_part.split("(", 1)[0].strip()

    import re
    days = hours = minutes = seconds = 0
    m = re.search(r"(\d+)\s*day", up_part)
    if m:
        days = int(m.group(1))
    m = re.search(r"(\d+)\s*hour", up_part)
    if m:
        hours = int(m.group(1))
    m = re.search(r"(\d+)\s*minute", up_part)
    if m:
        minutes = int(m.group(1))
    m = re.search(r"(\d+)\s*second", up_part)
    if m:
        seconds = int(m.group(1))

    if "About a minute" in up_part:
        minutes = 1
    if not (days or hours or minutes or seconds):
        return ""
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    return format_uptime(total)


def check_docker_containers():
    containers = {}
    try:
        r = subprocess.run(
            ["sudo", "docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=15
        )
        for line in r.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            name = parts[0]
            status = parts[1] if len(parts) > 1 else ""
            containers[name] = {
                "desc":   name,
                "alias":  alias_for(name),
                "running": "Up" in status,
                "status": status,
                "uptime": parse_docker_uptime(status),
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