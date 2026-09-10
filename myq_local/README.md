# myQ Local Bridge for HAOS

This app provides a local TLS-PSK MQTT endpoint for the owned myQ device and
creates a native Home Assistant Garage Door cover through MQTT discovery.

Designed as a HAOS Managed Container.

## Prerequisites

Using this MyQ bridge app is not trivial. You will need to read the flash on the
physical logic board to obtain the PSK required to connect an MQTT server.
This requires appropriate SPI-flash access and some confidence that you won't
damage your expensive 100$ logic board.

1. Install and start Home Assistant's official **Mosquitto broker** app.
2. Add the **MQTT** integration under **Settings > Devices & services**.
3. In the official Mosquitto app's Network section, clear its host mapping for
   port **8883** so this app can own that host port. Leave port 1883 unchanged.

## Installation as a local app

Copy the complete `myq_local` folder into `/addons/myq_local` on HAOS. Then:

1. Open **Settings > Apps > App store**.
2. Open the menu and select **Check for updates**.
3. Find **myQ Local Bridge** under Local apps and install it.
4. Enter the serial, device ID, and PSK for your opener in its Configuration tab.
5. Start the app and inspect its log.

The app does not ship with device credentials. You will have to obtain these
values from the device. The three device fields are required before it can
start:

!!! IMPORTANT CONFIGURATION

- `serial`: the ten-character myQ serial and TLS-PSK identity. Copy it exactly
  as recovered, including letter case; the bridge preserves it in MQTT topics.
- `device_id`: the six-byte door identifier written as twelve hexadecimal
  characters. Obtain it from the first six payload bytes of a door-state MQTT
  message in decrypted MITM traffic. The flash-reading commands only extract
  the serial and PSK. See step 10 of `FLASH_GUIDE.md` in the repository for the
  Wireshark procedure; an encrypted TLS capture alone will not expose the ID.
- `psk`: the opener's sixteen-byte TLS pre-shared key written as 32 hexadecimal
  characters.

The app is configured to mask the PSK field.

## Required DNS redirect and browser Wi-Fi setup

!!! IMPORTANT CONFIGURATION

**You must redirect DNS before configuring the opener's Wi-Fi. Use the
opener's browser setup page, not the myQ phone app, for this local setup.**

On the DNS server used by the myQ unit, override:

```text
connect.myqdevice.com -> HOME_ASSISTANT_IP
```

A hosts-file entry on another computer does not affect the myQ unit. Use the
router's local DNS feature, Pi-hole, AdGuard Home, or equivalent. Ensure the
opener's network uses that resolver. Redirect `connect.myqdevice.com`; the
separate `setup.myqdevice.com` address below is the opener's local setup page.

With the DNS rewrite active and myQ Local Bridge running:

1. Put the opener into Wi-Fi setup mode using your model's procedure. On the
   examined opener, press and release the Learn button three times.
2. Connect your phone or laptop directly to the opener's `MyQ-...` Wi-Fi
   network. Stay connected even if it reports no Internet access.
3. Open **http://setup.myqdevice.com/** in a browser, using HTTP. If the
   hostname does not resolve, open the gateway IP shown in the `MyQ-...`
   connection's network details using HTTP.
4. Select **Change Wi-Fi Settings**, choose your home Wi-Fi network, and enter
   its password. Use **Erase Wi-Fi Settings** first if necessary.
5. After the page confirms the network information was saved, select **Next**
   to connect the opener. Do not switch to the myQ app to finish setup.
6. Check your DNS query log for the redirected answer and the bridge log for
   the opener's connection and door state.

An already configured opener may keep using a previously resolved server IP;
changing DNS or power-cycling alone may not trigger a fresh lookup. Follow the
browser setup sequence above with the rewrite already active. The browser
saves Wi-Fi credentials separately from its Internet-connectivity result, so
use the router's client list and bridge logs to verify local connectivity.
The myQ app's cloud setup checks can interfere with this local-only workflow.

If your router supports it, block the device's direct Internet access to TCP
8883 while testing, while allowing it to reach HAOS on that port.

The app log should show a TLS client connection followed by:

```text
Door state: closed
```

MQTT discovery creates a device named **Garage Door** with a cover entity.

## State mapping

Version 0.1.3 rejects retained command replays and restores the latest cached
door state and attributes after reconnecting to Home Assistant MQTT. The cache
is held in memory for the lifetime of the app process; it does not reconstruct
unobserved changes while the physical opener is disconnected.

| Device value | Home Assistant state |
|---:|---|
| 1 | `open` |
| 2 | `closed` |
| 3 | `stopped` (inferred, not observed) |
| 4 | `opening` |
| 5 | `closing` |
