# syntax=docker/dockerfile:1
# Fleet harbor runner image + gc-cli External agent. Two additions vs the original:
#   1) gc-cli installed into harbor's tool venv (uv tool install --with);
#   2) the adapter (harbor/adapters/) baked in and put on PYTHONPATH.
#
# Build (needs an ssh-agent key with gitverse access, same as helpctl).
# ALWAYS pass GC_REF=<commit-sha> — it is what actually goes into the image (the code is
# cloned from gitverse@GC_REF, NOT from your local tree), and its value is part of the
# cache key, so changing it is what busts Docker's layer cache. Skip it (or pass the bare
# "master") and Docker serves the stale cached clone — this is exactly the KeyError we hit.
#
#   SHA=$(git rev-parse origin/master)          # or a specific commit you want to build
#   DOCKER_BUILDKIT=1 docker build --ssh default -f harbor/Dockerfile \
#     --build-arg GC_REF=$SHA \
#     -t registry.mlrnd.ru/docker-mlrnd/<ns>/harbor-gccli:$SHA .
#
# Tag with the same $SHA so the image name matches the code inside it.
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

# Which background-agent commit both the gc-cli package AND the adapter come from.
# Pass a commit SHA to pin+reproduce: `--build-arg GC_REF=<sha>`. Defaults to master.
# Threading ONE ref into both layers below is deliberate: it keeps the gc-cli framework
# and its harbor adapter from the SAME commit (no producer/consumer skew), and because the
# ARG value is part of each layer's cache key, bumping GC_REF busts the cache — a bare
# `@master` never changes textually, so Docker would serve a stale clone forever.
ARG GC_REF=master

# harbor + наш gc-cli (External-агент) в один tool-venv.
# gc-cli ставится рядом с harbor → harbor может импортить и фреймворк, и gc-cli.
RUN --mount=type=ssh \
    uv tool install harbor \
      --with "gc-ua @ git+ssh://git@gitverse.ru/gigacode-ai/background-agent@${GC_REF}"

# Адаптер запекаем в образ: клонируем и чекаутим тот же GC_REF (ветка ИЛИ SHA),
# кладём harbor/adapters на PYTHONPATH. PYTHONPATH наследуется и venv-питоном harbor →
# harbor видит --agent-import-path adapters.gc_cli_external:GcCliExternal
RUN --mount=type=ssh \
    git clone --filter=blob:none \
      ssh://git@gitverse.ru/gigacode-ai/background-agent.git /opt/gc-cli-src \
    && git -C /opt/gc-cli-src checkout "${GC_REF}" \
    && mkdir -p /opt/gc-harbor-adapters \
    && cp -r /opt/gc-cli-src/harbor/adapters /opt/gc-harbor-adapters/adapters \
    && cp -r /opt/gc-cli-src/prompts /opt/gc-harbor-adapters/prompts \
    && cp -r /opt/gc-cli-src/skills /opt/gc-harbor-adapters/skills \
    && rm -rf /opt/gc-cli-src
ENV PYTHONPATH=/opt/gc-harbor-adapters
