# Unprivileged nginx (UID 101); its stock server block listens on 8080, which
# is the port nginx.conf's `memory_ui` upstream and the k8s Service use.
FROM nginxinc/nginx-unprivileged:1.31.6-alpine3.24
COPY static/ /usr/share/nginx/html/
EXPOSE 8080
USER 101:101
