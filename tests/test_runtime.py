"""Exercise run.sh and two real brokers in an isolated, disposable container."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

import paho.mqtt.client as mqtt


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for MQTT condition")


class RuntimeTests(unittest.TestCase):
    def test_startup_tls_commands_and_reconnect(self):
        serial = "a1B2c3D4e5"
        psk = "00112233445566778899aabbccddeeff"
        state_topic = f"myq_local/{serial}/state"
        command_topic = f"myq_local/{serial}/command"
        device_topic = f"G/{serial}/011/0000"
        telemetry_topic = f"S/0F/{serial}/011/0000"
        messages = []
        connected = threading.Event()
        processes = []
        clients = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "ha.conf"
            config.write_text(
                "listener 18884 127.0.0.1\nallow_anonymous true\nuser root\n"
                f"persistence true\npersistence_location {root}/\n"
            )
            # Only Supervisor config/service lookups and logging are stubbed.
            # The production run.sh creates and starts its real PSK broker.
            shim = root / "bashio.sh"
            shim.write_text('''
bashio::config() {
    case "$1" in
        serial) printf '%s' "$TEST_SERIAL" ;;
        device_id) printf '112233445566' ;;
        psk) printf '%s' "$TEST_PSK" ;;
        name) printf 'Test Garage' ;;
        discovery_prefix) printf 'homeassistant' ;;
    esac
}
bashio::services.available() { return 0; }
bashio::services() {
    case "$2" in
        host) printf '127.0.0.1' ;;
        port) printf '18884' ;;
        username|password) printf 'test' ;;
    esac
}
bashio::log.info() { echo "$@"; }
bashio::log.error() { echo "$@" >&2; }
bashio::exit.nok() { echo "$@" >&2; exit 1; }
''')
            with (root / "runtime.log").open("w+") as log:
                def launch(args, **kwargs):
                    process = subprocess.Popen(args, stdout=log, stderr=log, **kwargs)
                    processes.append(process)
                    return process

                def start_ha():
                    return launch(["mosquitto", "-c", str(config)])

                def client(client_id, port, topic):
                    value = mqtt.Client(client_id=client_id)
                    value.on_message = lambda _c, _u, msg: messages.append((msg.topic, bytes(msg.payload)))

                    def on_connect(c, _u, _f, rc):
                        self.assertEqual(rc, 0)
                        c.subscribe(topic)
                        if port == 18884:
                            connected.set()

                    value.on_connect = on_connect
                    if port == 18884:
                        value.on_disconnect = lambda *_args: connected.clear()
                    value.connect_async("127.0.0.1", port, 10)
                    value.loop_start()
                    clients.append(value)
                    return value

                def tls_publish(payload):
                    subprocess.run([
                        "mosquitto_pub", "-h", "127.0.0.1", "-p", "8883",
                        "--psk", psk, "--psk-identity", serial,
                        "--tls-version", "tlsv1.2", "--ciphers", "PSK-AES128-CBC-SHA",
                        "-t", telemetry_topic, "-s",
                    ], input=payload, check=True, capture_output=True, timeout=10)

                try:
                    ha_process = start_ha()
                    observer = client("test-ha-observer", 18884, "#")
                    wait_until(connected.is_set)
                    app = launch(["bash", "/run.sh"], env={
                        **os.environ, "BASH_ENV": str(shim), "TEST_SERIAL": serial, "TEST_PSK": psk,
                    })
                    wait_until(lambda: any(t.endswith("/config") for t, _ in messages))
                    self.assertIsNone(app.poll())
                    self.assertEqual(Path("/run/myq-local/myq.psk").read_text(), f"{serial}:{psk}\n")
                    device = client("test-device-observer", 18883, f"G/{serial}/#")
                    wait_until(device.is_connected)
                    # The mixed-case identity must authenticate over actual TLS.
                    closed = bytes.fromhex("112233445566800200010200")
                    opened = bytes.fromhex("112233445566800200010100")
                    tls_publish(closed)
                    wait_until(lambda: (state_topic, b"closed") in messages)
                    # A live retained publish arrives with retain=False in
                    # MQTT 3.1.1; only its subsequent replay must be rejected.
                    observer.publish(command_topic, "OPEN", retain=True).wait_for_publish()
                    wait_until(lambda: (device_topic, opened) in messages)

                    # Keep device telemetry flowing while the HA broker is down.
                    ha_process.terminate()
                    ha_process.wait(timeout=10)
                    wait_until(lambda: not connected.is_set())
                    time.sleep(0.2)
                    tls_publish(opened)
                    # Device-side traffic never disconnects the bridge process.
                    self.assertIsNone(app.poll())
                    time.sleep(0.3)
                    messages.clear()
                    start_ha()
                    wait_until(lambda: (state_topic, b"open") in messages)
                    wait_until(lambda: any(t.endswith("/attributes") and json.loads(p).get("state_code") == 1
                                           for t, p in messages))
                    attrs = next(json.loads(p) for t, p in reversed(messages) if t.endswith("/attributes"))
                    self.assertEqual(attrs["state_code"], 1)
                    wait_until(lambda: "Ignoring retained Home Assistant command" in
                               (root / "runtime.log").read_text())
                    self.assertFalse(any(t == device_topic for t, _ in messages))
                except Exception:
                    log.flush()
                    log.seek(0)
                    print(log.read())
                    raise
                finally:
                    for value in clients:
                        value.disconnect()
                        value.loop_stop()
                    for process in reversed(processes):
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()

    def test_locked_packages_and_shell_syntax(self):
        result = subprocess.run(["apk", "info", "-v"], check=True, capture_output=True, text=True)
        actual = set()
        for line in result.stdout.splitlines():
            name, version, revision = line.rsplit("-", 2)
            actual.add(f"{name}={version}-{revision}")
        expected = set(Path("/app/apk.lock").read_text().splitlines())
        self.assertEqual(actual, expected)
        subprocess.run(["bash", "-n", "/run.sh"], check=True)


if __name__ == "__main__":
    unittest.main()
