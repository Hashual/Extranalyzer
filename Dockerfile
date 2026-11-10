# syntax=docker/dockerfile:1
FROM python:3.12-alpine AS build
WORKDIR /src
RUN pip install --no-cache-dir "setuptools>=68" wheel
COPY pyproject.toml ./
COPY app ./app
# Paquet local (Python pur) d'abord, puis ses dépendances en wheels binaires uniquement (aucune compilation).
RUN pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /w . \
 && pip wheel --no-cache-dir --only-binary=:all: --wheel-dir /w /w/extranalyzer-*.whl \
 && pip install --no-cache-dir --no-compile --no-index --find-links /w --prefix=/install /w/extranalyzer-*.whl

FROM python:3.12-alpine
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    EXTRANALYZER_DATA_DIR=/data
COPY --from=build /install /usr/local
RUN pip uninstall -y pip \
 && adduser -D -u 10001 app && mkdir /data && chown app:app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["uvicorn","--factory","app.main:get_app","--host","0.0.0.0","--port","8000","--workers","1","--no-access-log"]
