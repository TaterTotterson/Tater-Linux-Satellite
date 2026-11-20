"""PIN agent scoped to one explicit authenticated pairing request."""

# These annotations are D-Bus wire signatures, interpreted by @method at runtime.
# flake8: noqa: F821
# mypy: disable-error-code="name-defined"

from dbus_fast import DBusError
from dbus_fast.service import ServiceInterface, method


class PairingAgent(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.Agent1")
        self.device = ""
        self.pin = ""
        self.used = False

    def _pin(self, device):
        if device != self.device or len(self.pin) != 6:
            raise DBusError("org.bluez.Error.Rejected", "No pairing request for this device")
        self.used = True
        return self.pin

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        return int(self._pin(device))

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        return self._pin(device)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        if int(self._pin(device)) != passkey:
            raise DBusError("org.bluez.Error.Rejected", "Passkey does not match")

    @method()
    def RequestAuthorization(self, device: "o"):
        self._pin(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        self._pin(device)

    @method()
    def Cancel(self):
        self.device = self.pin = ""

    @method()
    def Release(self):
        self.device = self.pin = ""
