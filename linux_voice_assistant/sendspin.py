"""Optional official Sendspin player, supervised independently of voice reconnects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import shutil
import sys
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


class SendspinPlayer:
    def __init__(self, device_id: str, name: str) -> None:
        self.binary = os.environ.get("TATER_SENDSPIN_BINARY") or shutil.which("sendspin-cli")
        self.available = bool(self.binary and os.access(self.binary, os.X_OK))
        self.channel_selection = bool(self.binary and self.available and (Path(self.binary).resolve().parents[1] / "share/tater-sendspin-channel-routing").is_file())
        config = Path(os.environ.get("TATER_DATA_DIR") or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tater-satellite")
        self.directory = config / "sendspin" / hashlib.sha256(device_id.encode()).hexdigest()[:16]
        self.marker = self.directory / "playing"
        self.socket = self.directory / "control.sock"
        self.name = name
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._closing = False
        self.last_error = ""
        self.external = False
        self.app_name = "Tater-Sendspin-" + self.directory.name
        self.output_channel_mode = "stereo"
        self._duck_factor = 1.0
        self._duck_task: asyncio.Task[None] | None = None
        self._base_volumes: dict[str, list[int]] = {}

    def duck(self, factor: float) -> None:
        """Duck only this player's Pulse stream, preserving its chosen volume."""
        self._duck_factor = max(0.0, min(1.0, float(factor)))
        if self.available and shutil.which("pactl") and (self._duck_task is None or self._duck_task.done()):
            self._duck_task = asyncio.create_task(self._apply_duck())

    async def _pactl(self, *args):
        process = await asyncio.create_subprocess_exec("pactl", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise OSError("PulseAudio stream control failed")
        return output

    async def _apply_duck(self) -> None:
        try:
            while True:
                factor = self._duck_factor
                streams = json.loads(await self._pactl("--format=json", "list", "sink-inputs"))
                for stream in streams:
                    if stream.get("properties", {}).get("application.name") != self.app_name:
                        continue
                    index = str(stream["index"])
                    channels = stream.get("volume", {})
                    base = self._base_volumes.get(index)
                    if base is None:
                        base = [int(value["value"]) for value in channels.values()]
                        if not base:
                            continue
                        self._base_volumes[index] = base
                    await self._pactl("set-sink-input-volume", index, *(str(round(value * factor)) for value in base))
                    if factor == 1.0:
                        self._base_volumes.pop(index, None)
                if factor == self._duck_factor:
                    return
        except (OSError, ValueError, KeyError, asyncio.TimeoutError) as exc:
            _LOGGER.debug("Sendspin ducking unavailable: %s", exc)

    def start(self) -> None:
        if not self.external and self.available and not self._closing and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run())

    def restart(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
        else:
            self.start()

    async def _run(self) -> None:
        if not self.binary:
            return
        delay = 1
        while not self._closing:
            try:
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.marker.unlink(missing_ok=True)
                hook = shlex.join([sys.executable, "-m", "linux_voice_assistant.sendspin", str(self.marker)])
                args = [
                    self.binary,
                    "--name",
                    self.name,
                    "--mdns-name",
                    self.name,
                    "--manufacturer",
                    "Tater",
                    "--product-name",
                    "Tater Linux Satellite",
                    "--port",
                    "8928",
                    "--output",
                    "pulse",
                    "--buffer-ms",
                    "160",
                    "--audio-format",
                    "pcm:48000:16:2,flac:48000:16:2",
                    "--allow-unpaired",
                    "--state-dir",
                    str(self.directory),
                    "--control-socket",
                    str(self.socket),
                    "--hook-start",
                    hook + " start",
                    "--hook-stop",
                    hook + " stop",
                ]
                self._process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    env={**os.environ, "PULSE_PROP": "application.name=" + self.app_name + " media.role=music", "TATER_SENDSPIN_OUTPUT_CHANNEL": self.output_channel_mode},
                )
                self.last_error = ""
                result = await self._process.wait()
                if not self._closing:
                    self.last_error = f"Sendspin exited with status {result}"
                    _LOGGER.warning(self.last_error)
            except OSError as exc:
                self.last_error = str(exc)
                _LOGGER.warning("Could not start Sendspin: %s", exc)
            finally:
                self._process = None
                self.marker.unlink(missing_ok=True)
            if not self._closing:
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    def status(self) -> dict:
        return {
            "available": self.available,
            "running": self.external or bool(self._process and self._process.returncode is None),
            "playing": self.marker.exists(),
            "output_channel_mode": self.output_channel_mode,
            "last_error": self.last_error,
        }

    async def close(self) -> None:
        self.duck(1.0)
        if self._duck_task is not None:
            await self._duck_task
        if self.external:
            return
        self._closing = True
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.marker.unlink(missing_ok=True)


if __name__ == "__main__":
    # Hooks are launched by the official player, with the per-device path
    # already quoted by shlex. They never read credentials or spawn commands.
    marker = Path(sys.argv[1])
    if sys.argv[2] == "start":
        marker.touch(mode=0o600)
    elif sys.argv[2] == "stop":
        marker.unlink(missing_ok=True)
