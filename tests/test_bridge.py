"""Regression tests; run inside the pinned app image (see BUILDING.md)."""

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
ENV = {
    "SERIAL": "a1B2c3D4e5",
    "DEVICE_ID": "112233445566",
    "DEVICE_NAME": "Test Garage",
    "DISCOVERY_PREFIX": "homeassistant",
    "HA_MQTT_HOST": "127.0.0.1",
    "HA_MQTT_PORT": "1883",
    "HA_MQTT_USER": "test",
    "HA_MQTT_PASSWORD": "test",
}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("bridge_test", ROOT / "myq_local/bridge.py")
        self.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, ENV):
            spec.loader.exec_module(self.bridge)
        self.bridge.ha_client = Mock()
        self.bridge.device_client = Mock()
        for client in (self.bridge.ha_client, self.bridge.device_client):
            client.publish.return_value.rc = 0

    def message(self, topic, payload, retain=False):
        return SimpleNamespace(topic=topic, payload=payload, retain=retain)

    def state(self, code, device_id=None):
        b = self.bridge
        payload = (device_id or b.DEVICE_ID) + bytes.fromhex("80020001") + code.to_bytes(2, "little")
        b.on_device_message(None, None, self.message(b.MYQ_STATE_TOPIC, payload))

    def reconnect(self):
        b = self.bridge
        b.on_ha_connect(b.ha_client, None, {}, 0)

    def publications(self):
        return {call.args[0]: call.args[1] for call in self.bridge.ha_client.publish.call_args_list}

    def test_retained_commands_never_forwarded(self):
        b = self.bridge
        b.device_online.set()
        for payload in (b"OPEN", b"CLOSE"):
            b.on_ha_message(None, None, self.message(b.HA_COMMAND_TOPIC, payload, retain=True))
        b.device_client.publish.assert_not_called()

    def test_live_commands_and_offline_rejection(self):
        b = self.bridge
        b.on_ha_message(None, None, self.message(b.HA_COMMAND_TOPIC, b"OPEN"))
        b.device_client.publish.assert_not_called()
        b.device_online.set()
        for command, action in ((b"OPEN", b"0100"), (b"CLOSE", b"0000")):
            b.on_ha_message(None, None, self.message(b.HA_COMMAND_TOPIC, command))
            b.device_client.publish.assert_called_with(
                b.MYQ_COMMAND_TOPIC, b.DEVICE_ID + bytes.fromhex("80020001" + action.decode()),
                qos=0, retain=False,
            )

    def test_outage_caches_latest_state_and_merges_attributes(self):
        b = self.bridge
        self.state(2)
        self.state(1)
        counters = b"\x00" + (12).to_bytes(4, "little") * 2
        b.on_device_message(None, None, self.message(f"S/0F/{b.SERIAL}/019/0025", counters))
        b.ha_client.publish.reset_mock()
        self.reconnect()
        sent = self.publications()
        self.assertEqual(sent[b.HA_STATE_TOPIC], "open")
        attrs = json.loads(sent[b.HA_ATTRIBUTES_TOPIC])
        self.assertEqual(attrs["state_code"], 1)
        self.assertEqual(attrs["probable_cycle_counter"], 12)
        self.assertEqual(sent[b.HA_AVAILABILITY_TOPIC], "online")
        topics = [call.args[0] for call in b.ha_client.publish.call_args_list]
        self.assertLess(topics.index(b.HA_STATE_TOPIC), topics.index(b.HA_AVAILABILITY_TOPIC))
        self.assertTrue(all(call.kwargs["retain"] for call in b.ha_client.publish.call_args_list))

    def test_cached_reading_does_not_mark_disconnected_device_online(self):
        b = self.bridge
        self.state(2)
        b.on_device_disconnect(None, None, 1)
        self.reconnect()
        self.assertEqual(self.publications()[b.HA_AVAILABILITY_TOPIC], "offline")

    def test_invalid_telemetry_does_not_replace_cache(self):
        b = self.bridge
        self.state(2)
        self.state(1, bytes.fromhex("aabbccddeeff"))
        self.state(99)
        self.reconnect()
        self.assertEqual(self.publications()[b.HA_STATE_TOPIC], "closed")

    def test_no_invented_state_before_first_telemetry(self):
        self.reconnect()
        self.assertNotIn(self.bridge.HA_STATE_TOPIC, self.publications())

    def test_serial_identity_preserved(self):
        b = self.bridge
        self.assertEqual(b.SERIAL, ENV["SERIAL"])
        self.assertEqual(b.MYQ_COMMAND_TOPIC, "G/a1B2c3D4e5/011/0000")
        self.assertEqual(json.loads(b.discovery_payload())["device"]["serial_number"], ENV["SERIAL"])

    def test_startup_reply(self):
        b = self.bridge
        b.on_device_message(None, None, self.message(b.MYQ_STARTUP_TOPIC, b""))
        args, kwargs = b.device_client.publish.call_args
        self.assertEqual(args[0], b.MYQ_STARTUP_REPLY_TOPIC)
        self.assertEqual(len(args[1]), 32)
        self.assertFalse(kwargs["retain"])


if __name__ == "__main__":
    unittest.main()
