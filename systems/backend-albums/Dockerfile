FROM docker.io/library/python:3.12-slim

WORKDIR /app

# Install dependencies first (layer-cached)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app.py ./

# Port the server listens on (matches app.py default; override via PORT env var)
EXPOSE 5005

# Model used by Ollama — override at runtime with -e ALBUMS_LLM_MODEL=...
ENV ALBUMS_LLM_MODEL=granite3.3:8b
ENV FLASK_APP=app.py

# Production server rather than `flask run`, which is the development server.
# Long timeout: this service waits on an LLM to translate a prompt into SQL.
CMD ["gunicorn", "--workers=1", "--threads=2", "--timeout=600", \
     "--bind=0.0.0.0:5005", "app:app"]
