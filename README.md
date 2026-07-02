# Hermes Jobs Monitor

Hermes 定时任务与守护进程可视化监控面板。

![Dashboard](https://img.shields.io/badge/Python-3.11+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

## 功能

- 📊 **定时任务状态** — 名称、调度周期、上次/下次执行时间、状态、错误信息
- 🖥️ **系统服务监控** — HA WebSocket 守护进程、Docker 容器
- 🔄 **自动刷新** — 每 30 秒自动刷新
- 🎨 **深色主题** — 适配 Oracle 服务器背景

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python server.py

# 访问
open http://64.110.104.241:8899
```

## API

| 端点 | 说明 |
|------|------|
| `GET /api/all` | 所有数据（任务 + 系统） |
| `GET /api/jobs` | 定时任务列表 |
| `GET /api/system` | 系统服务状态 |

## 系统要求

- Python 3.8+
- Hermes Agent（用于 cron jobs 数据）
- systemctl（用于 systemd 服务查询）
- sudo docker（用于容器查询）