"""Identify the verified receiver game-input nodes, excluding vendor HID."""
import ctypes as C
from ctypes import wintypes as W
import re

def receiver_input_targets():
    """Identify only game input ancestors; never hide the composite/vendor HID."""
    from dsbridge.controllers.flydigi.apex6.hid import receiver_interfaces
    rows = [row for row in receiver_interfaces()
            if re.match(r"^HID\\VID_37D7&PID_2502&IG_[0-9A-F]{2}\\", row["instance_id"], re.I)]
    if len(rows) != 1:
        raise RuntimeError("需要连接唯一的八爪鱼 6 接收器")
    cfg = C.WinDLL("cfgmgr32.dll")
    cfg.CM_Locate_DevNodeW.argtypes = [C.POINTER(W.DWORD), W.LPWSTR, W.DWORD]
    cfg.CM_Get_Parent.argtypes = [C.POINTER(W.DWORD), W.DWORD, W.DWORD]
    cfg.CM_Get_Device_IDW.argtypes = [W.DWORD, W.LPWSTR, W.DWORD, W.DWORD]
    node = W.DWORD()
    if cfg.CM_Locate_DevNodeW(C.byref(node), rows[0]["instance_id"], 0):
        raise RuntimeError("接收器输入节点已断开")
    result = [rows[0]["instance_id"]]
    # Windows may assign IG_01 after reconnection; follow this input's own tree.
    group = re.search(r"&IG_([0-9A-F]{2})\\", rows[0]["instance_id"], re.I)[1]
    for prefix in ("USB\\VID_37D7&PID_2502&IG_" + group.upper() + "\\", "USB\\VID_37D7&PID_2502&MI_00\\"):
        parent, name = W.DWORD(), C.create_unicode_buffer(512)
        if cfg.CM_Get_Parent(C.byref(parent), node, 0) or cfg.CM_Get_Device_IDW(parent, name, len(name), 0):
            raise RuntimeError("无法确认接收器输入父节点")
        if not name.value.upper().startswith(prefix):
            raise RuntimeError("接收器输入父节点与已验证型号不符")
        result.append(name.value)
        node = parent
    return result
