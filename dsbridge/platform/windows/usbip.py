"""Release only a captured local VIIPER export, including driver retry attempts.

usbip-win2 0.9.7.7's installed SDK documents stop_attach_attempts(location)
and detach(port). Its official CLI exposes them as attach --stop and detach.
This does not change the native attachment path or reinstall/restart drivers.
"""
import json
import os
from pathlib import Path
import re
import subprocess


def cleanup_export(record, *, run=subprocess.run):
    bus, device, port = record["bus_id"], str(record["device_id"]), record.get("usbip_port")
    if not isinstance(bus, int) or not 1 <= bus <= 0xFFFFFFFF or not re.fullmatch(r"[0-9]{1,10}", device):
        raise ValueError("无效的本次虚拟设备记录")
    if port is not None and (not isinstance(port, int) or not 1 <= port <= 255):
        raise ValueError("无效的本次虚拟 USB 端口")
    cli = Path(os.environ.get("ProgramW6432", r"C:\Program Files")) / "USBip" / "usbip.exe"
    # The pinned VIIPER native code stores the literal host "localhost".
    # usbip-win2 compares the location string when cancelling attach attempts.
    location = "localhost:3241/" + str(bus) + "-" + device
    def call(args):
        result = run([str(cli)] + args, capture_output=True, timeout=8,
                     creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("USB 连接清理失败：" + (result.stderr or result.stdout).decode("utf-8", "replace").strip())
        return result.stdout.decode("utf-8", "replace")
    stop = ["--tcp-port", "3241", "attach", "--remote", "localhost", "--bus-id", str(bus) + "-" + device, "--stop"]
    result = {"location": location, "usbip_port": port, "detached": False}
    result["stopped_retries"] = call(stop).strip()
    if port:
        ports = call(["port"])
        if ports.strip() and not re.search(r"(?m)^Port\s+\d+:", ports):
            raise RuntimeError("虚拟 USB 端口列表格式未知，未移除任何端口")
        block = re.search(r"(?ms)^Port\s+0*" + str(port) + r":.*?(?=^Port\s+\d+:|\Z)", ports)
        listing = block.group(0) if block else ""
        result["port_listing"] = listing
        if listing.strip():
            # Never detach a port that has since been assigned to another export.
            exact = re.search(r"(?<![\w.:-])" + re.escape(location) + r"(?=$|[\s])", listing)
            if not exact:
                raise RuntimeError("虚拟 USB 端口已变更，未移除其他设备")
            result["detach_result"] = call(["detach", "--port", str(port)]).strip()
            result["detached"] = True
    result["stopped_retries_after_detach"] = call(stop).strip()
    return result


def record_attachment(base, bus, device, port):
    from dsbridge.controllers.flydigi.apex6.recovery import save_json
    record = dict(format=1, restore_pending=True, process_id=os.getpid(),
                  bus_id=bus, device_id=str(device), usbip_port=port)
    save_json(Path(base) / "viiper-usb-session.json", record)
    return record


def recover_export(base):
    from dsbridge.controllers.flydigi.apex6.recovery import save_json
    path = Path(base) / "viiper-usb-session.json"
    if not path.exists():
        return
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("format") != 1:
        raise RuntimeError("虚拟 USB 恢复记录格式异常")
    if record.get("restore_pending"):
        record["cleanup"] = cleanup_export(record)
        record["restore_pending"] = False
        save_json(path, record)
