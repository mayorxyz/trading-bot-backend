# Trading Bot Backend — production image.
# TA-Lib >= 0.6 ships manylinux wheels that bundle the C library,
# so no native build step is needed here.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY pyproject.toml readme.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Runtime state lives on the mounted volume, never in the image layers.
VOLUME ["/app/data"]

EXPOSE 8000

# Read-only API process; the live runner runs as its own service (see docker-compose.yml).
CMD ["python", "-m", "uvicorn", "tbb.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
