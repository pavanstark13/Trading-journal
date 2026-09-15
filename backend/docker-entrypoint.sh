#!/bin/sh
set -e

# `arq` workers must not run migrations; only the API container does.
case "$1" in
  uvicorn)
    echo "Applying database migrations..."
    alembic upgrade head
    ;;
esac

exec "$@"
