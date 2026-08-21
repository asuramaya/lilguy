# lilguy — Fast Edge Internship Aggregator & Ingestion Engine
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    NODE_VERSION=20

WORKDIR /app

# 1. Install system runtime dependencies & Node.js for Wrangler CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    libpq-dev \
    gcc \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_VERSION}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g wrangler@4 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy application codebase
COPY scraper/ /app/scraper/
COPY service/ /app/service/
COPY scripts/ /app/scripts/
COPY presets/ /app/presets/
COPY data/ /app/data/
COPY autofill/ /app/autofill/
COPY tests/ /app/tests/
COPY sources.yaml /app/sources.yaml
COPY filters.yaml /app/filters.yaml
COPY pytest.ini /app/pytest.ini
COPY README.md /app/README.md

RUN chmod +x /app/scripts/*.sh /app/scripts/*.py

EXPOSE 8000

ENTRYPOINT ["/app/scripts/container_worker.sh"]
CMD ["scheduler"]
