"""Bind selected XInput identity to present PnP game-input nodes, read-only.

Bluetooth ancestry takes precedence over USB ancestors (Bluetooth radios are
often USB). A wireless receiver is not a wired pad merely because it uses USB.
No button-state correlation, guessing among identical pads, or motor commands.
"""
import ctypes as C
import re
from dsbridge.core.connection import ControllerConnection

VIRTUAL_SERVICES = {"vigembus", "usbip2_ude", "vhidmini", "vjoy"}
APEX6_PRO_MODEL = "flydigi.apex6pro"
# Only an explicit device model name is useful here. Xbox Bluetooth IDs and
# generic names are shared by real Xbox pads and cannot identify an APEX.
_APEX6_PRO_NAME = re.compile(
    r"^(?:(?:flydigi|飞智)[\s_-]*)?(?:apex[\s_-]*6[\s_-]*pro|八爪鱼[\s_-]*6[\s_-]*pro)"
    r"(?=$|[\s/_（(]|影之刃零)", re.I)


def _bluetooth_model(row):
    names = [row.get("name", "")]
    for ancestor in row["ancestors"]:
        # Exclude the radio, USB hub and other machine ancestors. Their names
        # do not identify the selected physical controller.
        if ancestor["instance_id"].upper().startswith(("HID\\", "BTHENUM\\", "BTHLEDEVICE\\", "BTHLE\\")):
            names.append(ancestor.get("name", ""))
    return APEX6_PRO_MODEL if any(_APEX6_PRO_NAME.match(name.strip()) for name in names) else ""


def present_gamepads(vendor, product):
    from dsbridge.diagnostics.sony_writer import _Api, DWORD, GUID, SP_DEVINFO_DATA, SP_DEVICE_INTERFACE_DATA, INVALID_HANDLE_VALUE
    api = _Api()
    guid = GUID()
    api.hid.HidD_GetHidGuid(C.byref(guid))
    info = api.setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x12)
    if info in (None, INVALID_HANDLE_VALUE):
        raise OSError("无法读取 Windows 手柄接口")
    rows = []
    def prop(node, key):
        buffer = C.create_unicode_buffer(1024)
        size, kind = DWORD(C.sizeof(buffer)), DWORD()
        code = api.cfg.CM_Get_DevNode_Registry_PropertyW(node, key, C.byref(kind), buffer, C.byref(size), 0)
        return buffer.value if code == 0 else ""
    def ancestry(node):
        result = []
        for _ in range(20):
            instance = C.create_unicode_buffer(1024)
            if api.cfg.CM_Get_Device_IDW(node, instance, len(instance), 0):
                raise OSError("无法读取所选手柄的设备路径")
            result.append({"instance_id": instance.value, "service": prop(node, 5),
                           "name": prop(node, 13) or prop(node, 1)})
            parent = DWORD()
            if api.cfg.CM_Get_Parent(C.byref(parent), node, 0):
                break
            node = parent.value
        return result
    try:
        index = 0
        while True:
            interface = SP_DEVICE_INTERFACE_DATA(cbSize=C.sizeof(SP_DEVICE_INTERFACE_DATA))
            if not api.setup.SetupDiEnumDeviceInterfaces(info, None, C.byref(guid), index, C.byref(interface)):
                if C.get_last_error() == 259:
                    break
                raise OSError("手柄接口枚举不完整")
            index += 1
            needed = DWORD()
            api.setup.SetupDiGetDeviceInterfaceDetailW(info, C.byref(interface), None, 0, C.byref(needed), None)
            if not 8 <= needed.value <= 65536:
                continue
            detail = C.create_string_buffer(needed.value)
            DWORD.from_buffer(detail).value = 8 if C.sizeof(C.c_void_p) == 8 else 6
            node = SP_DEVINFO_DATA(cbSize=C.sizeof(SP_DEVINFO_DATA))
            if not api.setup.SetupDiGetDeviceInterfaceDetailW(info, C.byref(interface), detail, needed,
                                                            C.byref(needed), C.byref(node)):
                continue
            path = C.wstring_at(C.addressof(detail) + 4)
            # Bluetooth VID strings may include a four-digit source prefix.
            match = re.search(r"vid[_&](?:[0-9a-f]{4})?([0-9a-f]{4})&pid[_&]([0-9a-f]{4})", path, re.I)
            if not match or (int(match[1], 16), int(match[2], 16)) != (vendor, product):
                continue
            gamepad = "&ig_" in path.casefold()
            if not gamepad:
                handle = None
                try:
                    handle = api.open(path)
                    attrs, caps = api.inspect(handle)
                    gamepad = caps.UsagePage == 1 and caps.Usage in (4, 5)
                except (OSError, RuntimeError):
                    continue
                finally:
                    if handle not in (None, INVALID_HANDLE_VALUE):
                        api.kernel.CloseHandle(handle)
            if gamepad:
                ancestors = ancestry(node.DevInst)
                rows.append({"path": path, "instance_id": ancestors[0]["instance_id"],
                             "name": ancestors[0]["name"], "ancestors": ancestors})
    finally:
        api.setup.SetupDiDestroyDeviceInfoList(info)
    return rows


def classify_connection(identity, rows, *, flydigi=None):
    if identity is None:
        return ControllerConnection("disconnected", detail="请唤醒或重新连接手柄。")
    base = {"vendor_id": identity.get("vendor_id"), "product_id": identity.get("product_id")}
    if not all(base.values()):
        return ControllerConnection(detail="Windows 未提供所选槽位的设备身份，暂不推断连接方式。", **base)
    # A single HID input per selected VID/PID is required: multiple identical
    # devices must never cause another pad's transport to be silently selected.
    rows = list({row["instance_id"].casefold(): row for row in rows}.values())
    if len(rows) != 1:
        return ControllerConnection(detail="无法唯一对应所选手柄；请断开同型号的其他手柄后重新检测。" if rows
                                    else "未找到所选手柄的在线设备节点，请重新检测。", **base)
    row = rows[0]
    base.update(name=row["name"], device_id=row["instance_id"])
    ancestors = row["ancestors"]
    ids = [a["instance_id"].upper() for a in ancestors]
    services = {a.get("service", "").casefold() for a in ancestors}
    if services & VIRTUAL_SERVICES or any(i.startswith(("ROOT\\VIGEM", "ROOT\\VJOY")) for i in ids):
        return ControllerConnection("virtual", detail="请选择实体手柄。", **base)
    if any(i.startswith(("BTHENUM\\", "BTHLEDEVICE\\", "BTHLE\\")) for i in ids):
        model = _bluetooth_model(row)
        return ControllerConnection("bluetooth", detail="已核对所选手柄的蓝牙设备路径。",
                                    profile=1, model=model, model_source="device_name" if model else "", **base)
    if not any(i.startswith("USB\\VID_") for i in ids):
        return ControllerConnection(detail="设备路径未提供可确认的连接方式。", **base)
    if (base["vendor_id"], base["product_id"]) == (0x37D7, 0x2502):
        if flydigi and flydigi.get("kind") in ("receiver", "usb"):
            kind = flydigi["kind"]
            # SDL identifies 0x95 as APEX 6, 0x96 as Pro and 0x98 as the
            # Phantom Blade Zero Pro edition. The base model must stay generic.
            device_model = flydigi.get("model")
            model = APEX6_PRO_MODEL if type(device_model) is int and device_model in (0x96, 0x98) else ""
            return ControllerConnection(kind, detail="已读取八爪鱼当前连接状态。",
                                        profile=5 if kind == "receiver" else 1, model=model,
                                        model_source="protocol" if model else "", **base)
        return ControllerConnection(detail="八爪鱼连接状态尚未读到；请关闭其他手柄工具后重新检测。", **base)
    if identity.get("flags", 0) & 2 or any(re.search(r"\breceiver\b|接收器|\bdongle\b", a.get("name", ""), re.I) for a in ancestors[:4]):
        return ControllerConnection("receiver", detail="Windows 报告此手柄通过无线接收器连接。", profile=1, **base)
    return ControllerConnection("usb", detail="已核对所选手柄的 USB 设备路径。", profile=1, **base)


def detect_connection(identity):
    if identity is None or not identity.get("vendor_id") or not identity.get("product_id"):
        return classify_connection(identity, [])
    rows = present_gamepads(identity["vendor_id"], identity["product_id"])
    preliminary = classify_connection(identity, rows)
    # The pure classifier first proves that this is one nonvirtual physical
    # input. Never send a model query merely because a VID/PID matched.
    if ((identity["vendor_id"], identity["product_id"]) != (0x37D7, 0x2502)
            or preliminary.kind != "unknown" or not preliminary.device_id):
        return preliminary
    selected = next(row for row in rows if row["instance_id"].casefold() == preliminary.device_id.casefold())
    if not any(a["instance_id"].upper().startswith("USB\\VID_") for a in selected["ancestors"]):
        return preliminary
    from dsbridge.controllers.flydigi.apex6.connection import query_connection
    return classify_connection(identity, rows, flydigi=query_connection(selected))


def matches_selected_input(identity, expected, rows):
    """Compare one selected physical input without querying vendor commands."""
    if identity is None or not expected.device_id:
        return False
    pair = (identity.get("vendor_id"), identity.get("product_id"))
    if not all(pair) or pair != (expected.vendor_id, expected.product_id):
        return False
    current = classify_connection(identity, rows)
    return (current.kind not in ("disconnected", "virtual") and bool(current.device_id)
            and current.device_id.casefold() == expected.device_id.casefold())
