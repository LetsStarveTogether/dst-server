FROM ghcr.io/astral-sh/uv:latest AS uv

FROM docker.io/cm2network/steamcmd:latest
LABEL maintainer="wh2099@pm.me"

ARG DST_64_PKGS="ca-certificates libcurl3-gnutls procps"

WORKDIR /
VOLUME ["/cluster"]

# Install DST server dependencies.
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends ${DST_64_PKGS} && \
    apt-get -y clean && \
    rm -rf /var/lib/apt/lists/* && \
    install -d -o steam -g steam /install

# Install the DST server.
USER steam
ARG BETA=""
ARG GAME_VERSION
RUN set -e; \
    delay=10; \
    for attempt in 1 2 3 4 5 6 7 8 9 10; do \
        if "${STEAMCMDDIR}/steamcmd.sh" \
            +@ShutdownOnFailedCommand 1 \
            +@NoPromptForPassword 1 \
            +force_install_dir /install \
            +login anonymous \
            +app_update 343050 ${BETA:+ -beta updatebeta} validate \
            +quit > /tmp/steamcmd-install.log 2>&1; then \
            cat /tmp/steamcmd-install.log; \
            rm /tmp/steamcmd-install.log; \
            break; \
        else \
            status=$?; \
        fi; \
        cat /tmp/steamcmd-install.log || true; \
        echo "SteamCMD attempt ${attempt}/10 failed (exit ${status})." >&2; \
        for log in "${HOMEDIR}/Steam/logs/content_log.txt" "${HOMEDIR}/Steam/logs/connection_log.txt"; do \
            if [ -f "$log" ]; then cat "$log" || true; fi; \
        done; \
        [ "$attempt" -lt 10 ] && [ "$status" -lt 128 ] || exit "$status"; \
        case "$(cat /tmp/steamcmd-install.log)" in \
            *"ERROR! Failed to install app '343050' (Missing configuration)"*) ;; \
            *) exit "$status" ;; \
        esac; \
        echo "Retrying SteamCMD in ${delay}s..." >&2; \
        sleep "$delay"; \
        delay=30; \
    done; \
    installed_version="$(sed -n 's/\r$//; /^[0-9][0-9]*$/p' /install/version.txt)" && \
    if [ "${installed_version}" != "${GAME_VERSION}" ]; then \
        echo "Expected DST ${GAME_VERSION}, installed ${installed_version:-invalid}" >&2; \
        exit 1; \
    fi

# Install Python dependencies before the SDK to preserve the dependency layer.
USER root
ENV PATH="/app/.venv/bin:${PATH}" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY .python-version pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --extra otel --no-install-project --no-editable

COPY src ./src
RUN uv sync --locked --extra otel --no-editable

USER steam
WORKDIR /
CMD ["/app/.venv/bin/dst-server"]
