FROM ghcr.io/astral-sh/uv:latest AS uv

FROM docker.io/cm2network/steamcmd:root
LABEL maintainer="wh2099@pm.me"

ARG BETA=""
ARG DST_64_PKGS="ca-certificates libcurl3-gnutls procps"

ENV PATH="/app/.venv/bin:${PATH}" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python

WORKDIR /
VOLUME ["/cluster"]

COPY --from=uv /uv /uvx /bin/

# Install DST server dependencies.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ${DST_64_PKGS} && \
    apt-get -y clean && \
    rm -rf /var/lib/apt/lists/*

# Install the DST server.
RUN chmod u+w / && \
    chown -R root:root ${STEAMCMDDIR} && \
    ${STEAMCMDDIR}/steamcmd.sh \
        +@ShutdownOnFailedCommand 1 \
        +@NoPromptForPassword 1 \
        +force_install_dir /install \
        +login anonymous \
        +app_update 343050 ${BETA:+ -beta updatebeta} validate \
        +quit

# Install Python dependencies before the SDK to preserve the dependency layer.
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --extra otel --no-install-project --no-editable

COPY src ./src
RUN uv sync --locked --extra otel --no-editable

COPY entrypoint.py /entrypoint.py
WORKDIR /
CMD ["/app/.venv/bin/python", "/entrypoint.py"]
