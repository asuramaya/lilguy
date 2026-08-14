# One image, three roles (scheduler / api / discovery-cron), selected by
# the command docker-compose.yml passes each container -- keeps the
# image itself simple and avoids maintaining three near-identical
# Dockerfiles for code that all lives in the same repo and shares the
# same dependencies.
FROM python:3.12-slim

WORKDIR /app

COPY service/requirements.txt /app/service/requirements.txt
RUN pip install --no-cache-dir -r /app/service/requirements.txt

COPY scraper /app/scraper
COPY service /app/service
COPY sources.yaml /app/sources.yaml
COPY filters.yaml /app/filters.yaml
COPY presets /app/presets

ENV PYTHONUNBUFFERED=1

# No default CMD -- docker-compose.yml specifies scheduler.py, api.py
# (via uvicorn), or discovery's cron entrypoint per-service.
