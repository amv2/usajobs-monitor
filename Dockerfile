# usajobs-monitor in a container. See "Docker" in the README.
#
#   docker build -t usajobs-monitor .
#   docker run --rm -it -v ./data:/data usajobs-monitor init --profile me
#   docker compose up -d        # with compose.yaml copied from compose.example.yaml
#
# The base image is pinned by digest; Dependabot proposes updates.
# Build with optional extras, e.g. --build-arg EXTRAS=mcp or EXTRAS=ai,mcp.
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ARG EXTRAS=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Profiles live in /data/profiles, and each profile's reports beside it.
    USAJOBS_MONITOR_HOME=/data \
    # A container has no OS keychain; credentials come from environment variables.
    USAJOBS_KEYRING=0 \
    TZ=UTC

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".${EXTRAS:+[$EXTRAS]}" \
 && useradd --create-home --uid 1000 --user-group usajobs \
 && mkdir -p /data/profiles && chown -R usajobs:usajobs /data
COPY --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh

USER usajobs
VOLUME ["/data"]
WORKDIR /data
ENTRYPOINT ["entrypoint.sh"]
CMD ["--help"]
