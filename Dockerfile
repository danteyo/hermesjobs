FROM python:3.11-slim
WORKDIR /app
COPY server.py index.html ./
EXPOSE 8081
CMD ["python3", "-u", "server.py"]
