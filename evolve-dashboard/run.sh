#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

export MOMENTUM_PORT=8099
export MOMENTUM_DATA_DIR=/data
export MOMENTUM_WWW_DIR=/opt/momentum/www

MOMENTUM_LOG_LEVEL="$(bashio::config 'log_level' 'info')"
MOMENTUM_SESSION_DAYS="$(bashio::config 'session_days' '14')"
MOMENTUM_DAY_ROLLOVER_HOUR="$(bashio::config 'day_rollover_hour' '4')"
export MOMENTUM_LOG_LEVEL MOMENTUM_SESSION_DAYS MOMENTUM_DAY_ROLLOVER_HOUR

# Supervisor issues a scoped token; the app uses it to read the Mi scale and
# step entities through the core proxy. It never writes to Home Assistant.
export MOMENTUM_SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"

# Supervisor injects TZ from the Home Assistant system settings, so "today"
# lines up with the user's local midnight.
export MOMENTUM_TIMEZONE="${TZ:-UTC}"

bashio::log.info "Starting Momentum on port ${MOMENTUM_PORT} (TZ=${MOMENTUM_TIMEZONE})"

exec python3 /opt/momentum/server.py
