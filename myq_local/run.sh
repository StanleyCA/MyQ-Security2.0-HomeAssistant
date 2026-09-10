#!/usr/bin/with-contenv bashio
set -eu

# Preserve the device's exact TLS identity in both the PSK file and MQTT topics.
SERIAL="$(bashio::config 'serial')"
DEVICE_ID="$(bashio::config 'device_id')"
PSK="$(bashio::config 'psk')"
DEVICE_NAME="$(bashio::config 'name')"
DISCOVERY_PREFIX="$(bashio::config 'discovery_prefix')"

case "${SERIAL}" in
    *[!0-9A-Fa-f]*|'') bashio::exit.nok "serial must contain only hexadecimal characters" ;;
esac
case "${DEVICE_ID}" in
    *[!0-9A-Fa-f]*|'') bashio::exit.nok "device_id must contain exactly 12 hexadecimal characters" ;;
esac
case "${PSK}" in
    *[!0-9A-Fa-f]*|'') bashio::exit.nok "psk must contain exactly 32 hexadecimal characters" ;;
esac

if [ "${#SERIAL}" -ne 10 ]; then
    bashio::exit.nok "serial must contain exactly 10 hexadecimal characters"
fi
if [ "${#DEVICE_ID}" -ne 12 ]; then
    bashio::exit.nok "device_id must contain exactly 12 hexadecimal characters"
fi
if [ "${#PSK}" -ne 32 ]; then
    bashio::exit.nok "psk must contain exactly 32 hexadecimal characters"
fi

if ! bashio::services.available 'mqtt'; then
    bashio::exit.nok "Home Assistant MQTT service is unavailable; install and start the Mosquitto broker app"
fi

HA_MQTT_HOST="$(bashio::services mqtt 'host')"
HA_MQTT_PORT="$(bashio::services mqtt 'port')"
HA_MQTT_USER="$(bashio::services mqtt 'username')"
HA_MQTT_PASSWORD="$(bashio::services mqtt 'password')"

mkdir -p /run/myq-local
umask 077
printf '%s:%s\n' "${SERIAL}" "${PSK}" > /run/myq-local/myq.psk
chown mosquitto:mosquitto /run/myq-local/myq.psk
chmod 0600 /run/myq-local/myq.psk

cat > /run/myq-local/mosquitto.conf <<'EOF'
persistence false
connection_messages true
log_dest stdout
log_type error
log_type warning
log_type notice
log_type information
sys_interval 5
allow_anonymous true

# Only the bridge inside this add-on can reach this listener.
listener 18883 127.0.0.1
protocol mqtt

# HAOS publishes this listener as host port 8883 for the physical device.
listener 8883 0.0.0.0
protocol mqtt
tls_version tlsv1.2
ciphers PSK-AES128-CBC-SHA
psk_hint myq-local
psk_file /run/myq-local/myq.psk
EOF

export SERIAL DEVICE_ID DEVICE_NAME DISCOVERY_PREFIX
export HA_MQTT_HOST HA_MQTT_PORT HA_MQTT_USER HA_MQTT_PASSWORD

cleanup() {
    trap - TERM INT EXIT
    if [ -n "${BRIDGE_PID:-}" ]; then kill "${BRIDGE_PID}" 2>/dev/null || true; fi
    if [ -n "${BROKER_PID:-}" ]; then kill "${BROKER_PID}" 2>/dev/null || true; fi
    wait 2>/dev/null || true
}
trap cleanup TERM INT EXIT

bashio::log.info "Starting isolated myQ TLS-PSK broker on port 8883"
/usr/sbin/mosquitto -c /run/myq-local/mosquitto.conf &
BROKER_PID=$!

for _attempt in $(seq 1 20); do
    if mosquitto_pub -h 127.0.0.1 -p 18883 -t myq-local/selftest -m ready >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "${BROKER_PID}" 2>/dev/null; then
        bashio::exit.nok "The device-facing MQTT broker exited during startup"
    fi
    sleep 0.25
done

bashio::log.info "Starting Home Assistant MQTT discovery bridge"
python3 /app/bridge.py &
BRIDGE_PID=$!

while kill -0 "${BROKER_PID}" 2>/dev/null && kill -0 "${BRIDGE_PID}" 2>/dev/null; do
    sleep 5
done

bashio::log.error "A required process exited; stopping the add-on"
exit 1
