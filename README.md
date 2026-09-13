# myQ Security 2.0+ Local Bridge for Home Assistant

Control a Chamberlain/myQ garage-door opener locally through Home Assistant using its existing Wi-Fi module. The bridge provides a local TLS-PSK MQTT endpoint, translates the opener's binary messages, and creates a garage-door cover through Home Assistant MQTT discovery.

## What it supports

- Logic Board: 050DCTWF https://www.chamberlain.com/receiver-logic-board-dc-wifi/p/050DCTWF
- Open and close commands from Home Assistant.
- Reported door states: open, closed, opening, and closing.
- MQTT discovery and device availability reporting.
- The startup response required by the examined Wi-Fi/ARM firmware version **3.13**.
- Home Assistant OS (HAOS) local app installation on **amd64** and **aarch64**.

Compatibility is based on the hardware and firmware examined in the writeup; support for every myQ or Security 2.0+ model has not been established. The bridge configures one opener per app instance. Stop commands and position control are not implemented. State value `3` is mapped to `stopped`, but that interpretation has not been observed on the device.

## How it works

```text
myQ opener
    | connect.myqdevice.com resolves to the Home Assistant IP
    | MQTT over TLS 1.2 PSK, TCP 8883
    v
myQ Local Bridge app
    Isolated Mosquitto broker <-> Python protocol bridge
                                      |
                                      | Home Assistant MQTT service
                                      v
                              Home Assistant MQTT broker
                                      |
                                      v
                              Garage Door cover
```

The app runs its own device-facing broker. Its Python bridge connects to that broker through a loopback listener on port `18883` and to Home Assistant's MQTT broker using credentials supplied by the Supervisor.

## Prerequisites

- Start with the [FLASH_GUIDE](FLASH_GUIDE.md) to obtain the require PSK and Device ID. 
- A compatible opener and access to its physical logic board to read the SPI flash and recover its device-specific TLS pre-shared key (PSK). This requires hardware work; see the [writeup](WRITEUP.md) for the examined flash layouts and key recovery details.
- Home Assistant OS with the official **Mosquitto broker** app running and the **MQTT** integration configured.
- A DNS resolver used by the opener that can override `connect.myqdevice.com` to your Home Assistant IP. This is likely going to be your home router. This cannot be configured on the opener unfortunately. 
- The opener's myQ serial, located on the back side of the garage door opener

Device credentials and flash dumps are not included in this repository.

## Installation Guide

1. If not installed, install Mosquitto broker app on HomeHAssistant  
2. In the official Mosquitto broker app's **Network** settings, clear the host mapping for **8883** so myQ Local Bridge can use it. Leave port **1883** unchanged.
2. Copy the complete [`myq_local`](myq_local/) folder to `/addons/myq_local` on HAOS.
3. Open **Settings > Apps > App store**, select **Check for updates** from the menu, and install **myQ Local Bridge** under Local apps.
4. Enter your device credentials in the app's **Configuration** tab and start it. (requires SERIAL, DEVICE_ID, and PSK)
5. Configure the DNS override and the opener's Wi-Fi as described below.
6. Optional: Connect to Homekit

### Configuration

Obtain device id and psk from the flash guide before continuing with the instructions below  [FLASH_GUIDE](FLASH_GUIDE.md)

| Option | Required | Description |
|---|---|---|
| `serial` | Yes | Exactly 10 hexadecimal characters: copy the myQ serial exactly as recovered, including letter case. The same value is used for the TLS-PSK identity and MQTT topics. |
| `device_id` | Yes | The six-byte door identifier from decrypted MITM MQTT traffic, written as 12 hexadecimal characters. See [extraction instructions](FLASH_GUIDE.md#10-obtain-the-device-id-from-decrypted-mitm-traffic). |
| `psk` | Yes | The unwrapped 16-byte TLS PSK, written as 32 hexadecimal characters. The app masks this field. |
| `name` | No | Device name in Home Assistant; defaults to `Garage Door`. |
| `discovery_prefix` | No | MQTT discovery prefix; defaults to `homeassistant`. |

The serial comes from the `myq_sn` record in `psm_seed`; the PSK is recovered by unwrapping `myq_aes`. The door identifier is the first six bytes of the door command/state payload described in the [protocol writeup](WRITEUP.md). It is a separate field from the serial.

### DNS and Wi-Fi setup

**Apply the DNS override before configuring Wi-Fi, and use the opener's browser setup page.**

On the DNS resolver used by the opener, add:

```text
connect.myqdevice.com -> HOME_ASSISTANT_IP
```

A hosts-file entry on your computer will not change the opener's DNS resolution.

**With the override active and the bridge running**:

1. Put the opener into Wi-Fi setup mode using your model's procedure. On the examined opener, press and release the Learn button three times.
2. Connect a phone or laptop to the opener's `MyQ-...` Wi-Fi network.
3. Open `http://setup.myqdevice.com/` in a browser. If it does not resolve, open the Wi-Fi connection's gateway IP using HTTP.
4. Select **Change Wi-Fi Settings**, choose your home network, and enter its password. Use **Erase Wi-Fi Settings** first if needed.
5. After the network information is saved, select **Next** to connect. Complete this process in the browser; the myQ phone app's cloud setup checks can interfere with local setup.
6. Check the DNS and bridge logs to confirm the opener connects locally.

An already configured opener may continue using a cached cloud IP. Repeating browser Wi-Fi setup with the override already active can force a fresh lookup. See the [detailed setup guide](myq_local/README.md) for additional network guidance.

### Verify operation

The bridge log should show a TLS client connection, followed by messages such as:

```text
Sent myQ startup response (012/0040)
Door state: closed
```

Home Assistant should discover a device named **Garage Door** (or your configured name) with a garage-door cover. The cover accepts open and close commands and updates from device telemetry.

## Firmware analysis tools (for after you get a copy of the flash image)

The tools in [`analysis`](analysis/) operate on existing flash images or packet captures; they do not acquire a flash dump from hardware. You will need to obtain them separately They are separate from the installed HAOS app.

| Tool | Purpose | Python dependencies |
|---|---|---|
| [`fwtool.py`](analysis/fwtool.py) | Inspect Marvell firmware containers and partition tables, compare slots, translate addresses, and extract segments. | Standard library |
| [`psmtool.py`](analysis/psmtool.py) | Parse PSMv2 records and unwrap the device's `myq_aes` key from flash images. | Standard library |
| [`thumbxref.py`](analysis/thumbxref.py) | Disassemble Thumb-2 code and locate references or call targets. | `capstone` |
| [`decrypt_tls_psk_pcap.py`](analysis/decrypt_tls_psk_pcap.py) | Decrypt captures of the examined TLS 1.2 PSK/AES-CBC protocol and validate record MACs and padding. | `dpkt`, `pycryptodome` |

Run these examples from the repository root, replacing `read.bin` with your own flash image:

```sh
python analysis/fwtool.py read.bin --summary
python analysis/psmtool.py read.bin --partition psm_seed
python analysis/psmtool.py read.bin --name myq_sn --show-values
python analysis/psmtool.py read.bin --name myq_aes --unwrap-myq-aes
```

The last two commands print device credentials. Flash images can also contain current and obsolete Wi-Fi credentials and HomeKit setup material; keep those files and command outputs private.

For disassembly or capture analysis, install the optional dependencies in your Python environment:

```sh
python -m pip install capstone dpkt pycryptodome
python analysis/thumbxref.py --help
python analysis/decrypt_tls_psk_pcap.py --help
```

Use each tool's `--help` for available options. Firmware slot offsets vary between the examined flash layouts; use the firmware summary and writeup to select the appropriate `--slot` when translating addresses or disassembling code.

## Repository contents

Build versions, regression tests, and the two-architecture validation procedure
are documented in [BUILDING.md](BUILDING.md).

- [`myq_local/`](myq_local/): HAOS app manifest, container build, startup script, MQTT bridge, and app documentation.
- [`analysis/`](analysis/): Firmware, persistent-storage, disassembly, and capture-analysis utilities.
- [`WRITEUP.md`](WRITEUP.md): Reverse-engineering findings, flash layouts, TLS key recovery, and MQTT message formats.
