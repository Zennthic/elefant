# The unprivileged variant runs as UID 101 and listens above 1024, so the front
# door needs no root. Pinned to an exact nginx + Alpine release; Dependabot
# (.github/dependabot.yml) proposes the bumps.
FROM nginxinc/nginx-unprivileged:1.31.6-alpine3.24
COPY nginx.conf /etc/nginx/nginx.conf
EXPOSE 8080
USER 101:101
