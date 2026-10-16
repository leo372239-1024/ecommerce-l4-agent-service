"""Resolve local or registry images to immutable digests, without changing other projects."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {
    "PYTHON_IMAGE": "python:3.12.13-slim",
    "NODE_IMAGE": "node:24.15.0-alpine",
    "NGINX_IMAGE": "nginxinc/nginx-unprivileged:stable-alpine",
    "POSTGRES_IMAGE": "pgvector/pgvector:pg18",
    "REDIS_IMAGE": "redis:8.10.2-alpine",
    "ELASTICSEARCH_IMAGE": "docker.elastic.co/elasticsearch/elasticsearch:9.5.5",
    "OTEL_IMAGE": "otel/opentelemetry-collector-contrib:0.162.0",
    "JAEGER_IMAGE": "jaegertracing/jaeger:2.22.0",
    "PROMETHEUS_IMAGE": "prom/prometheus:v3.15.0",
    "GRAFANA_IMAGE": "grafana/grafana:13.2.3",
}


def main():
    entries = []
    for name, tag in IMAGES.items():
        result = subprocess.run(
            ["docker", "image", "inspect", tag], capture_output=True, text=True, check=False
        )
        if result.returncode == 0:
            digest = json.loads(result.stdout)[0]["RepoDigests"][0]
        else:
            remote = subprocess.run(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    tag,
                    "--format",
                    "{{json .Manifest}}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            repository = tag.rsplit(":", 1)[0]
            digest = repository + "@" + json.loads(remote.stdout)["digest"]
        if "@sha256:" not in digest:
            raise ValueError(f"Registry returned no immutable digest for {name}")
        entries.append(f"# {tag}\n{name}={digest}")
    (ROOT / "infra/images.env").write_text("\n".join(entries) + "\n", encoding="utf-8")
    print(f"Pinned {len(entries)} images in infra/images.env")


if __name__ == "__main__":
    main()
