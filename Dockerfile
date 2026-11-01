FROM python:3.13-slim

WORKDIR /app

# tzdata lets the TZ env var resolve to a real zone instead of falling back to UTC
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# [AI-GENERATED] Include work-metrics migration and stats modules in the image.
COPY app.py sync.py classify.py formatting.py database.py stats.py schema.sql ./
COPY templates/ templates/
COPY static/ static/

EXPOSE 5000

CMD ["python", "app.py"]
