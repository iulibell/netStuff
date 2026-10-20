# pysnmp 4.4.12 的同步 hlapi 依赖 asyncore（Python 3.12 已移除），锁定 3.11
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py trap_receiver.py devices.yaml ./

EXPOSE 9116 9117 162/udp
# 默认跑 exporter；trap-receiver 服务在 compose 里用 command 覆盖
CMD ["python", "exporter.py"]
