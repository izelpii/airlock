# One-shot: make the gateway's certificate with the application's own
# make-certs.sh, on first start only. compose/README.md, "The certificate".
#
# Build context: a folder that compose/ci/build.sh assembles from this one plus
# systems/gateway/make-certs.sh, copied from the same commit - the script runs
# here exactly as it does in the script install.

FROM docker.io/library/alpine:3.20
ARG AIRLOCK_VERSION=unknown
ARG AIRLOCK_REVISION=unknown
LABEL org.opencontainers.image.title="Airlock Photos - certs" \
      org.opencontainers.image.source="https://github.com/izelpii/airlock" \
      org.opencontainers.image.version="${AIRLOCK_VERSION}" \
      org.opencontainers.image.revision="${AIRLOCK_REVISION}" \
      org.opencontainers.image.licenses="LicenseRef-Airlock-Personal-NonCommercial" \
      org.opencontainers.image.description="Part of the Airlock Photos compose install - see compose/README.md"

# make-certs.sh is bash, and uses openssl and hostname; iproute2 is not needed
# because it runs with --no-auto-ip here.
RUN apk add --no-cache bash openssl coreutils

WORKDIR /work
COPY make-certs.sh /work/make-certs.sh
COPY run.sh /usr/local/bin/airlock-certs
RUN chmod 0755 /work/make-certs.sh /usr/local/bin/airlock-certs && mkdir -p /work/certs

ENTRYPOINT ["/usr/local/bin/airlock-certs"]
