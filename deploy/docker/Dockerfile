FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# bağımlılık katmanı önbelleklensin
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# registry (gate denetleyicisi) tek süreçlik HTTP sağlık ucu ile koşar;
# asıl iş CLI'dır — konteyner hem worker sandbox'ı hem gatekeeper'dır.
RUN useradd -m factory && mkdir -p /data && chown factory /data
USER factory
VOLUME /data
ENV FACTORY_WORKSPACE=/data
HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import factory, sqlite3,os; sqlite3.connect(os.environ['FACTORY_WORKSPACE']+'/registry.db').execute('select 1')" || exit 1
ENTRYPOINT ["factory"]
CMD ["--help"]
