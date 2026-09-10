# Changelog

## 0.1.3

- Ignore retained Home Assistant commands so reconnects cannot replay an old
  open or close request.
- Cache the latest door state and merged attributes during MQTT outages and
  republish them before availability on reconnect.
- Preserve the exact configured serial in both TLS identity and MQTT topics.
- Pin the multi-architecture base image by digest and all Alpine package
  versions in `apk.lock`.
- Add regression tests and an isolated two-broker runtime test covering TLS-PSK
  authentication, retained commands, and state recovery after a broker restart.

## 0.1.2

- Reply to the opener's startup announcement with the server MQTT publish
  required by firmware 3.13's 12-second connection check.
- Send a fresh 32-byte response on each startup, with retain disabled.

## 0.1.1

- Remove device-specific serial, device ID, and PSK defaults.
- Require users to enter their own opener credentials before the app starts.
- Replace device-specific integration-test identifiers with synthetic fixtures.

## 0.1.0

- Initial local TLS-PSK MQTT bridge and Home Assistant cover discovery.
