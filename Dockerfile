FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN addgroup --system --gid 1000 recipes \
    && adduser --system --uid 1000 --ingroup recipes --home /app recipes

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=recipes:recipes . .
RUN mkdir -p /app/data/media /app/staticfiles \
    && chown -R recipes:recipes /app/data /app/staticfiles \
    && chmod +x /app/docker-entrypoint.sh

USER recipes

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
