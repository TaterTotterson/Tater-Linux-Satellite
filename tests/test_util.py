from subprocess import CompletedProcess
from unittest.mock import patch

from linux_voice_assistant.util import get_default_interface


def test_get_default_interface_uses_netifaces_gateway() -> None:
    with patch(
        "linux_voice_assistant.util.netifaces.default_gateway",
        return_value={2: ("192.168.1.1", "eth0")},
    ):
        assert get_default_interface() == "eth0"


def test_get_default_interface_falls_back_to_macos_route() -> None:
    route = CompletedProcess(
        args=["route", "-n", "get", "default"],
        returncode=0,
        stdout="   route to: default\n  interface: en0\n",
        stderr="",
    )
    with (
        patch(
            "linux_voice_assistant.util.netifaces.default_gateway",
            side_effect=NotImplementedError("No implementation for `gateways()` yet"),
        ),
        patch("linux_voice_assistant.util.platform.system", return_value="Darwin"),
        patch("linux_voice_assistant.util.subprocess.run", return_value=route) as run,
    ):
        assert get_default_interface() == "en0"

    run.assert_called_once_with(
        ["route", "-n", "get", "default"],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )


def test_get_default_interface_returns_none_when_fallback_is_unavailable() -> None:
    with (
        patch(
            "linux_voice_assistant.util.netifaces.default_gateway",
            side_effect=NotImplementedError("No implementation for `gateways()` yet"),
        ),
        patch("linux_voice_assistant.util.platform.system", return_value="Linux"),
    ):
        assert get_default_interface() is None
