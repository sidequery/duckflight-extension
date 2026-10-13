FROM ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /e2e
COPY test/kubernetes/pyproject.toml test/kubernetes/uv.lock ./
RUN uv sync --frozen --no-dev
COPY test/docker_smoke.py ./
COPY test/kubernetes/client.py ./
COPY test/kubernetes/idle_client.py ./
ENV PATH="/e2e/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["uv", "run", "--frozen", "--offline", "--no-dev", "/e2e/client.py"]
