# Stage 1 backend image (S1-CTR-01..04).
# Contains only the bot backend runtime; never NapCat, .env, game data or local configs.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install runtime dependencies and copy the backend source.
COPY pyproject.toml ./
COPY src ./src
COPY bot.py ./bot.py
RUN python -m pip install --no-cache-dir .

# CJK fonts so pet-card rendering works outside Windows; font paths are
# resolved cross-platform in roco_pet_cards._font_candidates.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Bind all interfaces in the container; PORT stays user-configurable via env.
ENV HOST=0.0.0.0

# Non-root runtime user (S1-CTR-02). The data directory is owned by it so the
# named Compose volume inherits writable ownership on first mount.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8081

# Healthcheck uses the Python standard library — no curl/wget needed (S1-CTR-04).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,sys,urllib.request; port=os.environ.get('PORT','8081'); url='http://127.0.0.1:'+port+'/healthz'; sys.exit(0 if urllib.request.urlopen(url, timeout=3).status==200 else 1)"

CMD ["python", "bot.py"]
