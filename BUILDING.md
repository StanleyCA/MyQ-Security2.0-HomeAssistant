# Building and testing myQ Local Bridge

The app uses an immutable multi-architecture Home Assistant base image digest
and the complete Alpine package version set in [`myq_local/apk.lock`](myq_local/apk.lock).
The same lock is used for `linux/amd64` and `linux/arm64` (HAOS `aarch64`).

| Component | Locked version |
|---|---|
| Alpine Linux | 3.24.1 |
| Python | 3.14.7-r1 |
| Mosquitto and clients | 2.1.2-r1 |
| Paho MQTT Python | 1.6.1-r4 |

Paho 1.6.1 provides the MQTT 3.1.1 callback API used by the bridge. Updating
Paho to version 2 requires reviewing callback compatibility as part of the
dependency update.

The base index digest is
`sha256:93ef607824e3f27e868f11b10938283a98bf880ed57bcf8eaa81c6c2d521f6f5`.
The exact package versions include transitive dependencies and packages
already installed in that base image. Builds fail if a locked package version
is unavailable; they do not silently select a newer version. Alpine mirrors
can remove older packages, so long-term rebuilds also depend on retaining those
packages or the built image. This pins build inputs, not image timestamps or
byte-identical output.

## Build and test on Windows

Run from the repository root in PowerShell with Docker Desktop's Linux engine
running. ARM64 tests require emulation support when the host is amd64.

```powershell
docker build --platform linux/amd64 --build-arg BUILD_ARCH=amd64 --build-arg BUILD_VERSION=0.1.3 -t myq-local-test:amd64 myq_local
docker run --rm --platform linux/amd64 --mount "type=bind,source=$((Get-Location).Path),target=/src,readonly" --workdir /src --entrypoint python3 myq-local-test:amd64 -B -m unittest discover -s tests -v

docker build --platform linux/arm64 --build-arg BUILD_ARCH=aarch64 --build-arg BUILD_VERSION=0.1.3 -t myq-local-test:aarch64 myq_local
docker run --rm --platform linux/arm64 --mount "type=bind,source=$((Get-Location).Path),target=/src,readonly" --workdir /src --entrypoint python3 myq-local-test:aarch64 -B -m unittest discover -s tests -v
```

On a POSIX shell, replace `$((Get-Location).Path)` with `$(pwd)` in the mount.
The tests run inside disposable containers with no published host ports and no
physical opener connection. Do not run the runtime suite directly on HAOS:
it starts test brokers and uses the app's `/run/myq-local` paths.

## Test coverage

- Retained `OPEN` and `CLOSE` messages are ignored; live commands retain their
  binary encoding and offline devices reject commands.
- The most recent state and merged attributes survive HA MQTT outages within
  the running bridge process and are replayed before availability.
- Unknown state codes and telemetry for a different device do not replace the
  cached door state; cached readings do not make an offline device online.
- Mixed-case serials remain identical in MQTT topics, discovery, and the PSK
  file and authenticate through the real TLS-PSK listener.
- The production startup script starts a real Mosquitto broker and bridge.
  Only Supervisor configuration/service lookups and bashio logging are stubbed.
- A real HA broker restart replays an old retained command, which the bridge
  rejects, and restores state received during the outage.
- Installed package versions match `apk.lock`, and `run.sh` passes Bash syntax
  checking. Git enforces LF line endings for shell scripts.

These checks do not replace installation testing on HAOS or physical door
testing. The telemetry cache is in memory and does not survive an app restart.

For version 0.1.3, both image builds and all 10 tests passed on Docker Desktop's
Linux engine, with ARM64 executed through emulation on an amd64 host.

## Updating dependencies

Resolve a new base image to its multi-architecture digest, choose compatible
Alpine packages, and regenerate the complete `name=version` package inventory
from the resulting image. Update the Dockerfile digest and `apk.lock` together.
Rebuild and run the suite for both architectures before committing the new
pins. Keep the app manifest, discovery version, and changelog in sync.
