"""Read-only Windows driver and product checks."""
import hashlib
import os
from pathlib import Path
import platform
import subprocess
import sys

class WindowsProbe:
    """Read-only OS probes, separate from installation for fresh-machine tests."""
    def __init__(self, assets=None):
        self.assets = Path(assets) if assets else None
        program_files = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles", r"C:\Program Files")
        self.usbip = Path(program_files) / "USBip/usbip.exe"
        self.hidhide = Path(program_files) / "Nefarius Software Solutions/HidHide/x64/HidHideCLI.exe"

    def supported(self):
        return os.name == "nt" and platform.machine().upper() in ("AMD64", "X86_64") and sys.getwindowsversion().major >= 10

    @staticmethod
    def registry_value(key, name):
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
            return winreg.QueryValueEx(handle, name)[0]

    def identity(self):
        try:
            machine = self.registry_value(r"SOFTWARE\Microsoft\Cryptography", "MachineGuid")
            machine = hashlib.sha256(str(machine).encode()).hexdigest()
            boot = self.registry_value(r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters", "BootId")
            return {"machine": machine, "boot": str(boot)}
        except OSError:
            # If Windows does not expose BootId, runtime probes remain authoritative.
            return None

    def service(self, name):
        # Missing and inaccessible are deliberately different: access denied
        # must never be treated as permission to reinstall a present driver.
        try:
            self.registry_value("SYSTEM\\CurrentControlSet\\Services\\" + name, "Type")
        except FileNotFoundError:
            return False
        return True

    def product_exists(self, key):
        import winreg
        # Detect an incomplete/custom-path installation even if the usual CLI is absent.
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | view)
            except FileNotFoundError:
                continue
            with root:
                for index in range(winreg.QueryInfoKey(root)[0]):
                    with winreg.OpenKey(root, winreg.EnumKey(root, index)) as sub:
                        try:
                            name = str(winreg.QueryValueEx(sub, "DisplayName")[0]).casefold()
                        except FileNotFoundError:
                            continue
                        if (key == "hidhide" and "hidhide" in name) or (key == "usbip" and ("usbip" in name or "usb/ip" in name)):
                            return True
        return False

    def cli_exists(self, key):
        return (self.usbip if key == "usbip" else self.hidhide).is_file()

    def usbip_command(self, argument):
        result = subprocess.run([str(self.usbip), argument], capture_output=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode, (result.stdout + result.stderr).decode("utf-8", "replace").strip()

    def hidhide_ready(self):
        from dsbridge.platform.windows.hidhide import HidHideControl
        with HidHideControl() as driver:
            driver.snapshot()  # GET IOCTLs only; do not enable cloaking or change lists.



