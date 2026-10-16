ARG PYTHON_IMAGE=python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
FROM ${PYTHON_IMAGE} AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN pip install --no-cache-dir uv==0.11.19
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv lock --check && uv sync --frozen --no-dev --no-install-project

FROM build AS dependency-audit
RUN uv sync --frozen --no-install-project
RUN /opt/venv/bin/pip-audit -f cyclonedx-json -o /tmp/linux-dependencies.cdx.json

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 agent && useradd --uid 10001 --gid agent --create-home agent
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=agent:agent app ./app
COPY --chown=agent:agent migrations ./migrations
COPY --chown=agent:agent alembic.ini ./
USER agent

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]

