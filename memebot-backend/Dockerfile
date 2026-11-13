# MEMEBOT backend - DEMO ONLY (nessuna transazione reale)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
RUN useradd -m memebot && chown -R memebot /app
USER memebot
ENV PORT=8000 RUN_WORKERS=all
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health',timeout=8)"
# Un solo processo: API + WebSocket + scanner + motore paper trading (ideale per piani gratuiti).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
