FROM python:3.11-slim

WORKDIR /app

# No third-party deps (stdlib only)
COPY server.py index.html ./

EXPOSE 8081

CMD ["python3", "-u", "server.py", "--port", "8081"]
