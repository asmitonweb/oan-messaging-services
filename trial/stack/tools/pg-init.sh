#!/bin/sh
# Trial PostgreSQL bootstrap: owner role (migrations) and runtime role (least privilege).
set -eu
psql -v ON_ERROR_STOP=1 --username postgres <<SQL
CREATE ROLE oan_msg_owner LOGIN PASSWORD '${OAN_OWNER_PW}';
CREATE ROLE oan_msg_runtime LOGIN PASSWORD '${OAN_RUNTIME_PW}';
CREATE DATABASE oan_messaging OWNER oan_msg_owner;
SQL
psql -v ON_ERROR_STOP=1 --username postgres -d oan_messaging -c "ALTER SCHEMA public OWNER TO oan_msg_owner"
