# The compose gateway: the application's gateway image, with a configuration
# for a compose network. compose/images/gateway/render-config.py says what
# changes and why; everything else is the application's.
#
# Build context: compose/images.   BASE_IMAGE: the application's gateway,
# built unchanged from systems/gateway.

ARG BASE_IMAGE

FROM ${BASE_IMAGE} AS base

FROM docker.io/library/python:3.12-alpine AS render
COPY --from=base /etc/nginx/templates/airlock.conf.template /in/airlock.conf.template
COPY gateway/render-config.py /render-config.py
RUN python3 /render-config.py /in/airlock.conf.template > /airlock.conf.template

FROM base
ARG AIRLOCK_VERSION=unknown
ARG AIRLOCK_REVISION=unknown
LABEL org.opencontainers.image.title="Airlock Photos - gateway" \
      org.opencontainers.image.source="https://github.com/izelpii/airlock" \
      org.opencontainers.image.version="${AIRLOCK_VERSION}" \
      org.opencontainers.image.revision="${AIRLOCK_REVISION}" \
      org.opencontainers.image.licenses="LicenseRef-Airlock-Personal-NonCommercial" \
      org.opencontainers.image.description="Part of the Airlock Photos compose install - see compose/README.md"

COPY --from=render /airlock.conf.template /etc/nginx/templates/airlock.conf.template
COPY gateway/16-airlock-ollama.envsh /docker-entrypoint.d/16-airlock-ollama.envsh
RUN chmod 0755 /docker-entrypoint.d/16-airlock-ollama.envsh

# 15-local-resolvers.envsh (part of the nginx image) exports the container's DNS
# server as NGINX_LOCAL_RESOLVERS when this is set.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
ENV NGINX_ENVSUBST_FILTER=(SERVER_DOMAIN|NGINX_LOCAL_RESOLVERS|AIRLOCK_OLLAMA_SERVER)
