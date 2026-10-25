# ── airlock gateway ───────────────────────────────────────────────────────────
# One nginx on :80 that routes by Host header, so nothing in the application has
# to know a port number. See documentation/specs/redesign-ports-routes-spec.md.
#
# This is deliberately a SEPARATE image from the frontend, even though the
# frontend is also nginx. The frontend image rebuilds whenever src/ changes, and
# routing config must not be coupled to a React build - that coupling is what
# made schema changes invisible for weeks (CONTAINERIZATION_TRADEOFFS.md).

FROM docker.io/library/nginx:1.27-alpine

# Where the upstreams live. host.containers.internal from inside a pod;
# override to localhost to run this image with --network=host, or when the
# gateway runs directly on the machine.
ENV UPSTREAM_HOST=host.containers.internal

# The browser-facing name. Changing this is how the server moves to a public
# domain (user-authentication-spec.md section 10.7): an environment variable at
# container start, not an image rebuild. The frontend bundle is unaffected -
# every URL in it is relative.
ENV SERVER_DOMAIN=airlock.lan

# The nginx image runs envsubst over /etc/nginx/templates/*.template at start
# and writes the result into /etc/nginx/conf.d/. The filter is a regex matched
# against variable NAMES, restricting substitution to ours so nginx's own $host,
# $remote_addr and $connection_upgrade survive untouched.
ENV NGINX_ENVSUBST_FILTER=(UPSTREAM_HOST|SERVER_DOMAIN)
ENV NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx/conf.d

COPY airlock.conf.template /etc/nginx/templates/airlock.conf.template
COPY proxy.conf            /etc/nginx/proxy.conf

# The stock config ships a default server on :80 that would shadow ours.
RUN rm -f /etc/nginx/conf.d/default.conf

# Certificates are mounted at run time from systems/gateway/certs (make-certs.sh).
# They are environment state, like catalogs.db, and are never baked into an image.
# /var/www/acme serves HTTP-01 challenges when a public domain is added.
RUN mkdir -p /var/www/acme /etc/nginx/certs

EXPOSE 80 443

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/gateway-health || exit 1

CMD ["nginx", "-g", "daemon off;"]
