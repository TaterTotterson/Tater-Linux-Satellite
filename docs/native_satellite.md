# Native satellite settings

These settings apply when connecting to Tater or the Tater Home Assistant bridge.

## Pairing and saved settings

The base HTTP URL is converted to Tater's native satellite WebSocket endpoint
automatically. After the first successful connection, the durable device token
is saved to `--tater-token-file`; later starts can omit `--tater-token`.

The satellite reconnects automatically when Tater or the network becomes
available again.

Updated Tater and HA bridge controllers can save a new satellite name and room
live, without resetting or pairing again. These values are restored on startup
from `tater-identity.json` beside the preferences file. Set
`--tater-identity-file` to use another JSON file; only `name` and `room` are
changed, preserving connection settings and unknown keys. ThirdReality uses
its existing `/data/conf/tater.json`. A blank room clears the assignment.

When native mode connects, Tater sends the device's current Voice settings.
Changes made later in Tater are applied without restarting the satellite and
are cached for the next boot. The bundled Hey Tater model and wake sounds work
offline; catalog/custom wake words and custom sounds are downloaded once and
then reused from the satellite's download directory.

When STT Wake Verification is enabled in Tater, the satellite keeps a short
rolling microphone window and asks Tater to confirm the detected phrase before
playing the wake sound or opening the conversation. Observe mode records the
same checks without delaying or rejecting wakes. Verification errors and
timeouts fail open so an unavailable STT service cannot disable the satellite.

On supported Linux satellites, Bluetooth stays off when unused. An explicit
**Scan for devices** in Tater or the HA bridge starts a bounded discovery window;
opening the Bluetooth page does not scan. Discovery pauses during voice activity
and stops automatically. Up to two active GATT connections can remain open for
devices such as Meshtastic, and keep the radio enabled until disconnected.
Saved bonds alone do not start Bluetooth. Broadcast-only sensors are not
monitored continuously, and Bluetooth speaker audio is outside this proxy.

The proxy requires BlueZ (`bluetoothd`), a system D-Bus connection, a BLE adapter,
and the Linux Bleak dependency installed with this project. The service user
must have permission to manage that adapter. It owns the adapter's power state;
use a dedicated adapter if the host also uses Bluetooth for unrelated devices.
Missing hardware or permission errors leave voice service running. Bonds stay
in BlueZ; authenticated bond markers (never PINs) use
`~/.local/share/tater/bluetooth_bonds.json`, overridable with `TATER_BLE_BONDS_PATH`.

## Audio And Wake Words

List available audio devices:

```sh
./script/run --list-input-devices
./script/run --list-output-devices
```

Select devices when starting the satellite:

```sh
./script/run \
  --audio-input-device "INPUT DEVICE" \
  --audio-output-device "OUTPUT DEVICE" \
  --tater-url http://tater.local:8501 \
  --tater-token-file ~/.config/tater-linux-satellite/device-token
```

Wake-word models use a matching `.json` configuration and `.tflite` model.
Browse shared models and custom-word tooling in
[Tater Wake Words](https://github.com/TaterTotterson/Tater-Wake-Words), then
select an installed model with `--wake-model`.

Run `./script/run --help` for all audio, wake-word, sound, timer, and peripheral
options.

### Shared wake and audio features

Install with `./script/setup --onnx` to enable the ONNX openWakeWord and
NanoWakeWord lanes. Choose the detector, CNN/GRU architecture, sensitivity,
and environment profile in Tater's satellite settings. Bundles supply the
recommended thresholds and patience; explicit tuning overrides are retained.

The bundled Hey Tater endpoint model uses the same boundary policy as Native
and Echo. Custom bundles can include their own boundary model. If no matching
endpoint is available, the satellite keeps the audio rather than guessing a cut.
The separate STT wake check still uses its configured audio window and timeout.

The Docker image includes Sendspin. For a regular installation, install the
Debian dependencies listed in `script/install-sendspin`, then run that script
with a writable `--prefix` and put its `bin` directory on `PATH`. The player
starts in Tater native mode when available and keeps its pairing state across
voice reconnects. `TATER_DATA_DIR` optionally sets its configuration directory.

Playback interruption requires a microphone or audio backend that cancels
speaker echo. Use `--microphone-has-aec` only with such hardware; Tater can then
enable wake interruption during replies. S420's firmware adapter supplies its
own playback reference and manages its own Sendspin service.

## Tater Settings

| Command-line option | Environment variable | Purpose |
| --- | --- | --- |
| `--tater-url` | `TATER_URL` | Tater base URL or native WebSocket URL |
| `--tater-token` | `TATER_TOKEN` | One-time pairing code or existing device token |
| `--tater-token-file` | `TATER_TOKEN_FILE` | Loads and stores the durable paired token |
| `--tater-device-id` | `TATER_DEVICE_ID` | Stable ID reported to Tater |
| `--tater-board` | `TATER_BOARD` | Hardware identifier, such as `linux` or `reachy_mini` |
| `--tater-room` | `TATER_ROOM` | Optional room reported to Tater |
| `--tater-identity-file` | `TATER_IDENTITY_FILE` | Persistent name/room JSON; defaults beside the preferences file |
| `--tater-reconnect-seconds` | `TATER_RECONNECT_SECONDS` | Delay between reconnect attempts |

See [.env.example](../.env.example) for audio, wake-word, sound, and runtime
settings.

## Reachy Mini projects

- [Reachy Home Assistant Satellite](https://huggingface.co/spaces/TaterTotterson/reachy_home_assistant_satellite) — the preserved ESPHome-style Home Assistant satellite.
- [Tater Reachy Standalone](https://huggingface.co/spaces/TaterTotterson/tater_reachy_standalone) — Tater and its satellite running together.
- [Reachy Tater Embedded](https://huggingface.co/spaces/TaterTotterson/reachy_tater_embedded) — the embedded Tater experience on Reachy Mini.
