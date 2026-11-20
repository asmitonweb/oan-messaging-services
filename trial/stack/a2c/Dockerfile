# Frappe v16 bench for the A2C integration trial (Python 3.14, Node 24, MariaDB client).
FROM node:24-bookworm-slim AS node
FROM python:3.14-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
 && apt-get update && apt-get install -y --no-install-recommends \
      git curl build-essential pkg-config mariadb-client libmariadb-dev redis-tools libffi-dev \
      libssl-dev cron \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g yarn@1 \
 && pip install --no-cache-dir frappe-bench \
 && useradd -m -s /bin/bash frappe
USER frappe
WORKDIR /home/frappe
CMD ["sleep", "infinity"]
