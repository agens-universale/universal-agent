# syntax=docker/dockerfile:1
# Fleet harbor runner image + gc-cli External agent. Two additions vs the original:
#   1) gc-cli installed into harbor's tool venv (uv tool install --with);
#   2) the adapter (harbor/adapters/) baked in and put on PYTHONPATH.
#
# Build (needs an ssh-agent key with gitverse access, same as helpctl):
#   DOCKER_BUILDKIT=1 docker build --ssh default -f harbor/Dockerfile \
#     -t registry.mlrnd.ru/docker-mlrnd/<ns>/harbor-gccli:gccli_v0.1 .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/root/.local/bin:${PATH}"

ARG DEBIAN_FRONTEND=noninteractive
ARG POLAR_REF=stable
ARG DOCKER_COMPOSE_VERSION=v2.32.4
RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends -qq \
    build-essential \
    ca-certificates \
    curl \
    docker-cli \
    git \
    openssh-client \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-linux-$(uname -m)" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
    && docker compose version \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# helpctl — git+ssh://git@gitverse.ru/gigacode-ai/helpctl.git@0.3.5
# Сборка: DOCKER_BUILDKIT=1 docker build --ssh default …
RUN mkdir -p -m 0700 /root/.ssh \
    && ssh-keyscan -H gitverse.ru >> /root/.ssh/known_hosts

RUN --mount=type=ssh \
    uv pip install --system "helpctl @ git+ssh://git@gitverse.ru/gigacode-ai/helpctl.git@0.3.5"

# Polar (ProRL-Agent-Server): https://github.com/NVIDIA-NeMo/ProRL-Agent-Server
RUN uv pip install --system "polar @ git+https://github.com/NVIDIA-NeMo/ProRL-Agent-Server.git@${POLAR_REF}"

# harbor + наш gc-cli (External-агент) в один tool-venv.
# gc-cli ставится рядом с harbor → harbor может импортить и фреймворк, и gc-cli.
RUN --mount=type=ssh \
    uv tool install harbor \
      --with "gc-cli-pub @ git+ssh://git@gitverse.ru/gigacode-ai/background-agent@feat/harbor-external-adapter"

# Адаптер запекаем в образ: клонируем ветку, кладём harbor/adapters на PYTHONPATH.
# PYTHONPATH наследуется и venv-питоном harbor → harbor видит
# --agent-import-path adapters.gc_cli_external:GcCliExternal
RUN --mount=type=ssh \
    git clone --depth 1 --branch feat/harbor-external-adapter \
      ssh://git@gitverse.ru/gigacode-ai/background-agent.git /opt/gc-cli-src \
    && mkdir -p /opt/gc-harbor-adapters \
    && cp -r /opt/gc-cli-src/harbor/adapters /opt/gc-harbor-adapters/adapters \
    && rm -rf /opt/gc-cli-src
ENV PYTHONPATH=/opt/gc-harbor-adapters
