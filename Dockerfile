FROM python:3.12-slim

ARG UV_VERSION=0.10.8
ARG THETATRAP_BUILD_SHA=unknown
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    THETATRAP_BUILD_SHA=${THETATRAP_BUILD_SHA}

LABEL org.opencontainers.image.title="ThetaTrap" \
      org.opencontainers.image.description="MCP-native Alpaca paper-options agent" \
      org.opencontainers.image.revision="${THETATRAP_BUILD_SHA}"

RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev --no-editable

RUN addgroup --system thetatrap \
    && adduser --system --ingroup thetatrap --home /app thetatrap \
    && mkdir -p /data \
    && chown -R thetatrap:thetatrap /app /data

ENV PATH="/app/.venv/bin:${PATH}"
USER thetatrap

CMD ["python", "-m", "thetatrap.cli", "worker"]
