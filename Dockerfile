# Official uv base image. Bundles Python 3.12 + uv. Bookworm (Debian stable)
# picked for manylinux wheel compatibility (highspy, duckdb). Pin the uv
# image by its Python minor + distro so upstream uv version bumps don't
# silently change build behaviour.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

LABEL maintainer="Sam <sam@banksian.au>"
LABEL description="Amber wholesale energy optimiser for Sigenergy battery"

# System deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends tini && \
    rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd -m -r -s /bin/false optimiser

WORKDIR /app

# uv config:
# - UV_COMPILE_BYTECODE=1 precompiles .pyc at build time for faster startup.
# - UV_LINK_MODE=copy avoids symlink issues when /app is a volume.
# - UV_PYTHON_DOWNLOADS=0 forces use of the image's system Python — don't
#   let uv pull a different interpreter at runtime, that defeats the lock.
# - UV_NO_DEV=1 keeps dev-group deps (pytest, ruff, freezegun) out of
#   the production image.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_NO_DEV=1

# Install dependencies using the lockfile, BEFORE copying the source, so
# the layer cache only invalidates when lock/pyproject change. Bind-mounts
# the lock and pyproject so they don't end up duplicated in layers.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Copy the project and sync it in (installs energy-optimiser itself into
# the venv so the entry-point scripts are on PATH).
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

# Put the venv's bin at the front of PATH so `energy-optimiser`, `eo-smoke`,
# and `eo-replay` are callable by name.
ENV PATH="/app/.venv/bin:$PATH"

# Data directories
RUN mkdir -p /var/lib/energy-optimiser/snapshots && \
    mkdir -p /etc/energy-optimiser && \
    chown -R optimiser:optimiser /var/lib/energy-optimiser /app/.venv

# Volumes
VOLUME ["/etc/energy-optimiser", "/var/lib/energy-optimiser"]

USER optimiser

# tini as PID 1 for proper signal handling; use the entry-point script
# rather than `python -m optimiser` so the behaviour is consistent with
# how operators will invoke eo-smoke and eo-replay manually.
ENTRYPOINT ["tini", "--"]
CMD ["energy-optimiser", "--config", "/etc/energy-optimiser/config.toml"]
