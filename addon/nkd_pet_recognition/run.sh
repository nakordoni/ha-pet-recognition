#!/usr/bin/with-contenv bashio
# Options are read by the watcher from /data/options.json. Never echo them here.
bashio::log.info "Starting NKD Pet Recognition watcher"
exec python3 -u /app/watcher.py
