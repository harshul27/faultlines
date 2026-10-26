FROM python:3.13-slim
WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY *.py targets.json ./
COPY calib calib
COPY web web
RUN useradd -m app
USER app
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
