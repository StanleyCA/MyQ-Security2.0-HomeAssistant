#!/usr/bin/env python3
"""Bridge proprietary myQ MQTT bytes to a Home Assistant MQTT cover."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger("myq-local")

# The TLS identity and MQTT topics must use the same device-provided spelling.
SERIAL = os.environ["SERIAL"]
DEVICE_ID = bytes.fromhex(os.environ["DEVICE_ID"])
DEVICE_NAME = os.environ["DEVICE_NAME"]
DISCOVERY_PREFIX = os.environ["DISCOVERY_PREFIX"].strip("/")

HA_HOST = os.environ["HA_MQTT_HOST"]
HA_PORT = int(os.environ["HA_MQTT_PORT"])
HA_USER = os.environ["HA_MQTT_USER"]
HA_PASSWORD = os.environ["HA_MQTT_PASSWORD"]

BASE = f"myq_local/{SERIAL}"
HA_COMMAND_TOPIC = f"{BASE}/command"
HA_STATE_TOPIC = f"{BASE}/state"
HA_AVAILABILITY_TOPIC = f"{BASE}/availability"
HA_ATTRIBUTES_TOPIC = f"{BASE}/attributes"
DISCOVERY_TOPIC = f"{DISCOVERY_PREFIX}/cover/myq_local_{SERIAL}/config"

MYQ_COMMAND_TOPIC = f"G/{SERIAL}/011/0000"
MYQ_SUBSCRIPTION = f"S/0F/{SERIAL}/#"
MYQ_STATE_TOPIC = f"S/0F/{SERIAL}/011/0000"
MYQ_STARTUP_TOPIC = f"S/0F/{SERIAL}/003/0000"
MYQ_STARTUP_REPLY_TOPIC = f"G/{SERIAL}/012/0040"

STATE_NAMES = {1: "open", 2: "closed", 3: "stopped", 4: "opening", 5: "closing"}

stop_event = threading.Event()
device_online = threading.Event()
ha_connected = threading.Event()
telemetry_lock = threading.RLock()
latest_state: str | None = None
latest_attributes: dict = {}


def command_payload(open_door: bool) -> bytes:
    value = 1 if open_door else 0
    return DEVICE_ID + bytes.fromhex("80020001") + value.to_bytes(2, "little")


def discovery_payload() -> str:
    value = {
        "name": None,
        "unique_id": f"myq_local_{SERIAL}",
        "command_topic": HA_COMMAND_TOPIC,
        "state_topic": HA_STATE_TOPIC,
        "availability_topic": HA_AVAILABILITY_TOPIC,
        "json_attributes_topic": HA_ATTRIBUTES_TOPIC,
        "payload_open": "OPEN",
        "payload_close": "CLOSE",
        "state_open": "open",
        "state_closed": "closed",
        "state_opening": "opening",
        "state_closing": "closing",
        "state_stopped": "stopped",
        "device_class": "garage",
        "optimistic": False,
        "qos": 0,
        "retain": False,
        "device": {
            "identifiers": [f"myq_local_{SERIAL}"],
            "name": DEVICE_NAME,
            "manufacturer": "Chamberlain",
            "model": "myQ (local firmware protocol)",
            "serial_number": SERIAL,
        },
        "origin": {
            "name": "myQ Local Bridge",
            "sw_version": "0.1.3",
        },
    }
    return json.dumps(value, separators=(",", ":"))


def publish_ha(topic: str, payload: str, retain: bool = False) -> None:
    if not ha_connected.is_set():
        LOG.warning("Cannot publish %s while Home Assistant MQTT is disconnected", topic)
        return
    info = ha_client.publish(topic, payload, qos=0, retain=retain)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        LOG.warning("Publish to %s failed with result %s", topic, info.rc)


def set_availability(online: bool) -> None:
    with telemetry_lock:
        if online:
            device_online.set()
        else:
            device_online.clear()
        publish_ha(HA_AVAILABILITY_TOPIC, "online" if online else "offline", retain=True)


def update_telemetry(state: str | None = None, attributes: dict | None = None) -> None:
    """Keep the latest readings even while HA MQTT is unavailable."""
    global latest_state
    with telemetry_lock:
        if state is not None:
            latest_state = state
        if attributes is not None:
            latest_attributes.update(attributes)
        if not ha_connected.is_set():
            return
        if state is not None:
            publish_ha(HA_STATE_TOPIC, latest_state, retain=True)
        if attributes is not None:
            publish_ha(HA_ATTRIBUTES_TOPIC, json.dumps(latest_attributes), retain=True)


def on_ha_connect(client, _userdata, _flags, result_code):
    if result_code != 0:
        LOG.error("Home Assistant MQTT rejected connection: %s", result_code)
        return
    LOG.info("Connected to Home Assistant MQTT at %s:%d", HA_HOST, HA_PORT)
    # Serialize replay with device telemetry so older cached values cannot
    # overwrite a fresh update from the other MQTT thread.
    with telemetry_lock:
        ha_connected.set()
        client.subscribe(HA_COMMAND_TOPIC, qos=0)
        client.publish(DISCOVERY_TOPIC, discovery_payload(), qos=0, retain=True)
        if latest_state is not None:
            publish_ha(HA_STATE_TOPIC, latest_state, retain=True)
        if latest_attributes:
            publish_ha(HA_ATTRIBUTES_TOPIC, json.dumps(latest_attributes), retain=True)
        publish_ha(
            HA_AVAILABILITY_TOPIC,
            "online" if device_online.is_set() else "offline",
            retain=True,
        )


def on_ha_disconnect(_client, _userdata, result_code):
    with telemetry_lock:
        ha_connected.clear()
    if result_code != 0:
        LOG.warning("Home Assistant MQTT disconnected unexpectedly: %s", result_code)


def on_ha_message(_client, _userdata, message):
    if message.retain:
        LOG.warning("Ignoring retained Home Assistant command")
        return
    command = message.payload.strip().upper()
    if command not in (b"OPEN", b"CLOSE"):
        LOG.warning("Ignoring unsupported Home Assistant command: %r", message.payload)
        return
    if not device_online.is_set():
        LOG.warning("Refusing %s command because the physical myQ device is offline", command.decode())
        return
    payload = command_payload(command == b"OPEN")
    result = device_client.publish(MYQ_COMMAND_TOPIC, payload, qos=0, retain=False)
    if result.rc == mqtt.MQTT_ERR_SUCCESS:
        LOG.info("Published supervised door command: %s", command.decode())
    else:
        LOG.error("Door command publish failed: %s", result.rc)


def on_device_connect(client, _userdata, _flags, result_code):
    if result_code != 0:
        LOG.error("Internal device broker rejected bridge connection: %s", result_code)
        return
    LOG.info("Connected to isolated device broker")
    client.subscribe([(MYQ_SUBSCRIPTION, 0), ("$SYS/broker/clients/connected", 0)])


def on_device_disconnect(_client, _userdata, result_code):
    set_availability(False)
    if result_code != 0:
        LOG.warning("Internal device broker disconnected unexpectedly: %s", result_code)


def on_device_message(_client, _userdata, message):
    topic = message.topic
    payload = bytes(message.payload)

    if topic == "$SYS/broker/clients/connected":
        try:
            # One client is this bridge; the second is the physical myQ unit.
            set_availability(int(payload) >= 2)
        except ValueError:
            LOG.warning("Invalid broker client count: %r", payload)
        return

    # Any device-originated application packet proves current connectivity.
    set_availability(True)

    if topic == MYQ_STARTUP_TOPIC and payload == b"" and not message.retain:
        # Firmware 3.13 requires a server PUBLISH within 12 seconds of MQTT
        # startup. The cloud sends 32 bytes on 012/0040; this firmware ignores
        # their contents, but receiving the PUBLISH completes its online check.
        result = device_client.publish(
            MYQ_STARTUP_REPLY_TOPIC, os.urandom(32), qos=0, retain=False
        )
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            LOG.info("Sent myQ startup response (012/0040)")
        else:
            LOG.error("myQ startup response publish failed: %s", result.rc)
        return

    if topic == MYQ_STATE_TOPIC and len(payload) == 12 and payload[:6] == DEVICE_ID:
        state_code = int.from_bytes(payload[10:12], "little")
        state = STATE_NAMES.get(state_code)
        if state:
            update_telemetry(
                state=state,
                attributes={"state_code": state_code, "raw": payload.hex()},
            )
            LOG.info("Door state: %s", state)
        else:
            LOG.warning("Unknown door state code %d in %s", state_code, payload.hex())
        return

    if topic.endswith("/004/0000") and payload == b"\x00":
        set_availability(False)
        LOG.info("Physical myQ device published its offline will")
        return

    if topic.endswith("/019/0025") and len(payload) >= 9:
        first = int.from_bytes(payload[1:5], "little")
        second = int.from_bytes(payload[5:9], "little")
        update_telemetry(
            attributes={"probable_cycle_counter": first, "cycle_counter_copy": second},
        )


def handle_signal(_signum, _frame):
    stop_event.set()


# The Dockerfile pins paho-mqtt 1.6.1, which uses these MQTT v3 callbacks.
ha_client = mqtt.Client(client_id=f"myq-ha-{SERIAL}", clean_session=True)
ha_client.username_pw_set(HA_USER, HA_PASSWORD)
ha_client.will_set(HA_AVAILABILITY_TOPIC, "offline", qos=0, retain=True)
ha_client.on_connect = on_ha_connect
ha_client.on_disconnect = on_ha_disconnect
ha_client.on_message = on_ha_message

device_client = mqtt.Client(client_id=f"myq-device-bridge-{SERIAL}", clean_session=True)
device_client.on_connect = on_device_connect
device_client.on_disconnect = on_device_disconnect
device_client.on_message = on_device_message


def main() -> int:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    ha_client.connect_async(HA_HOST, HA_PORT, keepalive=30)
    device_client.connect_async("127.0.0.1", 18883, keepalive=30)
    ha_client.loop_start()
    device_client.loop_start()
    try:
        while not stop_event.wait(1):
            pass
    finally:
        set_availability(False)
        time.sleep(0.1)
        device_client.disconnect()
        ha_client.disconnect()
        device_client.loop_stop()
        ha_client.loop_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
