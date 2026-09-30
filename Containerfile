ARG GAME_VERSION
ARG BETA=""
ARG SOURCE_REVISION=""
ARG NATIVE_SCRIPTS_REVISION=""

FROM docker.io/library/rust:1.99.0-trixie AS rust-toolchain
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM docker.io/cm2network/steamcmd:latest AS game-base
LABEL maintainer="wh2099@pm.me"
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates libcurl3t64-gnutls procps && \
    rm -rf /var/lib/apt/lists/* && \
    install -d -o steam -g steam /install

# Compile against the same libc as the game and the final Agent image.
FROM game-base AS sdk-build
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential capnproto git python3 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:${PATH}
WORKDIR /build
COPY . .
RUN cargo build --locked --release -p dst-server --bin dst-server
ARG GAME_VERSION
ARG BETA
ARG SOURCE_REVISION
ARG NATIVE_SCRIPTS_REVISION
RUN install -D target/release/dst-server /out/native/dst-server-linux-x86_64 && \
    cp LICENSE /out/native/LICENSE && \
    python3 tools/build_manifest.py --output /out/build-manifest.json \
        --revision="${SOURCE_REVISION}" \
        --native-scripts-revision="${NATIVE_SCRIPTS_REVISION}" \
        --game-version="${GAME_VERSION}" ${BETA:+--beta} \
        --artifact /out/native/dst-server-linux-x86_64

# Python wheel tools are used only while building distributions.
FROM sdk-build AS distributions
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy
RUN uv python install 3.14 && \
    uv build --python 3.14 --out-dir /out/python --no-sources \
        -C 'maturin.build-args=--compatibility pypi' && \
    python3 tools/build_manifest.py --output /out/build-manifest.json \
        --revision="${SOURCE_REVISION}" \
        --native-scripts-revision="${NATIVE_SCRIPTS_REVISION}" \
        --game-version="${GAME_VERSION}" ${BETA:+--beta} \
        --python "$(uv python find 3.14)" \
        --artifact /out/native/dst-server-linux-x86_64 \
        --artifact /out/python/*.whl --artifact /out/python/*.tar.gz

FROM game-base AS game
# Install the DST server.
USER steam
ARG BETA
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

# The Agent and Lua bundle are compiled from the same SDK source.
USER root
COPY --from=sdk-build /out/native/dst-server-linux-x86_64 /usr/local/bin/dst-server
COPY --from=sdk-build /out/build-manifest.json /usr/local/share/dst-server/build-manifest.json
RUN dst-server scripts build /install/data/databundles/scripts.zip \
        --output /install/data/databundles/scripts.zip && \
    dst-server scripts verify /install/data/databundles/scripts.zip

USER steam
WORKDIR /
VOLUME ["/cluster"]
CMD ["/usr/local/bin/dst-server", "agent", "--cluster", "/cluster"]
