FROM denoland/deno:bin AS deno

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_ROOT=/data \
    USERS_ROOT=/users \
    LOG_LEVEL=INFO

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

RUN python -m pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn>=0.34,<1"

RUN groupadd --gid 1000 musicasyt \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin musicasyt \
    && mkdir -p /app /data /users \
    && chown -R 1000:1000 /app /data /users

WORKDIR /app
COPY --chown=1000:1000 app/ /app/app/
USER 1000:1000
EXPOSE 8080
CMD ["python", "-m", "app.serve"]
