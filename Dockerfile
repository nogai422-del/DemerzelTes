FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data && chmod 777 /app/data

EXPOSE 7196
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','7196')+'/health',timeout=4)"
CMD ["python", "-u", "launcher.py"]
