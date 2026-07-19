FROM python:3.14.6-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ROOT=/app

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

COPY assets ./assets
COPY deploy ./deploy

STOPSIGNAL SIGTERM
CMD ["python", "-m", "mydrecshop"]
