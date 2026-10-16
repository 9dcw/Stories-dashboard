FROM python:3.11-slim@sha256:f2f0261a2d316f8347d8e7643567766304010c6ac3ffeb56471ca2f21aff1aab

RUN groupadd -r stories && useradd -r -g stories -d /app stories

WORKDIR /app

# Install pinned dependencies from lock file
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r requirements.lock

# Copy application code (excluding story-data/, which is a volume mount)
COPY api/ /app/api/
COPY story_store.py /app/
COPY export_stories.py /app/
COPY collectors.py /app/
COPY source_fetch.py /app/
COPY polling.py /app/
COPY polling_sources.py /app/
COPY gist.py /app/
COPY summarize_items.py /app/
COPY source_discovery.py /app/
COPY schema.sql /app/
COPY scripts/ /app/scripts/

# Static frontend
COPY index.html /app/
COPY app.js /app/
COPY styles.css /app/
COPY sw.js /app/
COPY manifest.json /app/
COPY data/ /app/data/

# Entrypoint
COPY deploy/entrypoint.sh /app/deploy/entrypoint.sh
RUN chmod +x /app/deploy/entrypoint.sh

RUN mkdir -p /app/story-data && chown stories:stories /app/story-data

USER stories
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
CMD ["uvicorn", "api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]