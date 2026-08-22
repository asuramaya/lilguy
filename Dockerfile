# lilguy — Fast Edge Internship Aggregator & Ingestion Engine
FROM python:3.13-slim

WORKDIR /app

COPY service/requirements.txt /app/service/requirements.txt
RUN pip install --no-cache-dir -r /app/service/requirements.txt

COPY scraper /app/scraper
COPY service /app/service
COPY scripts /app/scripts
COPY presets /app/presets
COPY data /app/data
COPY sources.yaml /app/sources.yaml
COPY filters.yaml /app/filters.yaml

ENV PYTHONUNBUFFERED=1

# No default ENTRYPOINT or CMD -- docker-compose.yml specifies scheduler.py, api.py
# (via uvicorn), or discovery's entrypoint per-service.
