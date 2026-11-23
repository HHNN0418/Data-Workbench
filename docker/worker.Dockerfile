FROM python:3.13-alpine
WORKDIR /app
COPY ctn_data/proxy_worker.py /app/proxy_worker.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/proxy_worker.py"]
