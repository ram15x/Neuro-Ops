FROM python:3.15-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# the demo recordings come from the simulator, so they are built into the image
RUN python -m eval.export_replay > /dev/null

# non-root user, /data is where the sqlite file and the audit log live
RUN useradd -m -u 1000 neuroops && mkdir /data && chown -R neuroops:neuroops /app /data
VOLUME /data
ENV DB_URL=sqlite:////data/neuroops.db REDIS_FAKE=true
USER neuroops

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/ping', timeout=2).status == 200 else 1)"

# Run the application
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
