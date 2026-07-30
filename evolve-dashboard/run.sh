#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

export EVOLVE_PORT=8099
export EVOLVE_DATA_DIR=/data
export EVOLVE_WWW_DIR=/opt/evolve/www

EVOLVE_LOG_LEVEL="$(bashio::config 'log_level' 'info')"
EVOLVE_WEEK_STARTS_ON="$(bashio::config 'week_starts_on' 'monday')"
EVOLVE_DAY_ROLLOVER_HOUR="$(bashio::config 'day_rollover_hour' '4')"
EVOLVE_SENSORS="$(bashio::config 'sensors')"
export EVOLVE_LOG_LEVEL EVOLVE_WEEK_STARTS_ON EVOLVE_DAY_ROLLOVER_HOUR EVOLVE_SENSORS

# Supervisor hands us a scoped token; the app uses it to read entity states
# through the core proxy when the user has configured sensor tiles.
export EVOLVE_SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"

# Supervisor injects TZ into add-on containers from the Home Assistant
# system settings, so "today" lines up with the user's local midnight
# without this add-on needing Supervisor API access of its own.
export EVOLVE_TIMEZONE="${TZ:-UTC}"

bashio::log.info "Starting Self Improvement Dashboard on port ${EVOLVE_PORT} (TZ=${EVOLVE_TIMEZONE})"

exec python3 /opt/evolve/server.py
