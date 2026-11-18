FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home app && mkdir /data && chown app /data
USER app
# Mount your paywatch.toml at /data/paywatch.toml. The database is created next to it.
WORKDIR /data
VOLUME /data

CMD ["paywatch", "watch", "--config", "/data/paywatch.toml"]
