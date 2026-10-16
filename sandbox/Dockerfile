ARG PYTHON_IMAGE=python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 sandbox && useradd --uid 10001 --gid 10001 sandbox \
    && mkdir -p /run/sandbox && chown 10001:10001 /run/sandbox
WORKDIR /sandbox
COPY broker.py runner.py health.py ./
USER 10001:10001
CMD ["python", "-I", "-B", "broker.py"]
