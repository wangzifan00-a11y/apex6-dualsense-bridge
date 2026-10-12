"""Read-only, fail-closed selection of one controller's restartable input branch.

The caller supplies an exact HID instance, never a restart root. This module
does not alter PnP or HidHide state. A privileged helper must independently
repeat the validation immediately before it changes a device.

Node records used by the pure classifier contain instance_id, parent, children,
service, class_guid, hardware_ids and compatible_ids. children must be complete
for every node in the selected branch. Other ancestors need no child listing.
"""
from __future__ import annotations

import ctypes as C
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import sys
from collections.abc import Mapping

from dsbridge.core.connection import ControllerConnection

HID_CLASS = "{745a17a0-74d3-11d0-b6fe-00a0c90f57da}"
XUSB_CLASS = "{d61ca365-5af4-4486-998b-9db4734c6ca3}"
BT_HID_SERVICE = "{00001124-0000-1000-8000-00805f9b34fb}"
_APEX_INPUT = re.compile(r"HID\\VID_37D7&PID_2502&IG_([0-9A-F]{2})\\[^\\]+", re.I)
_BT_PROFILE = re.compile(r"BTHENUM\\" + re.escape(BT_HID_SERVICE) +
                         r"_VID&[0-9A-F]{8}_PID&[0-9A-F]{4}\\[^\\]+", re.I)
_VID_PID = re.compile(r"VID[_&](?:[0-9A-F]{4})?([0-9A-F]{4})[_&]PID[_&]([0-9A-F]{4})", re.I)
_VIRTUAL_SERVICES = {"vigembus", "usbip2_ude", "vhidmini", "vjoy", "rootvhid"}
_UNSAFE_SERVICES = {"kbdhid", "mouhid", "kbdclass", "mouclass"}
_GAME_IDS = {"hid_device_system_game", "hid_device_up:0001_u:0004", "hid_device_up:0001_u:0005"}


class InputTargetsError(RuntimeError):
    """The selected controller cannot be identified safely right now."""


class UnsupportedInputTargets(InputTargetsError):
    """The selected transport/layout has not been validated for a restart."""


@dataclass(frozen=True)
class InputTargets:
    device_id: str
    restart_id: str
    hidden_ids: tuple[str, ...]
    device_ids: tuple[str, ...]
    transport: str

    def to_dict(self):
        result = asdict(self)
        result["hidden_ids"] = list(self.hidden_ids)
        result["device_ids"] = list(self.device_ids)
        return result


def _selection(selected):
    if isinstance(selected, ControllerConnection):
        if selected.kind not in ("usb", "receiver", "bluetooth", "unknown"):
            raise InputTargetsError("热切换需要已连接的实体手柄。")
        device_id = selected.device_id
    else:
        device_id = selected
    if (not isinstance(device_id, str) or not 1 <= len(device_id) <= 1024
            or not re.fullmatch(r"HID\\[^\\\x00-\x1f]+\\[^\\\x00-\x1f]+", device_id, re.I)
            or any(c in device_id for c in "*?")):
        raise InputTargetsError("请提供所选手柄的完整 HID 实例路径，不能指定父设备或通配符。")
    return device_id


def _ids(node, field):
    value = node.get(field)
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) or not v or "\0" in v for v in value):
        raise InputTargetsError("手柄设备树信息不完整，请重新检测。")
    return tuple(value)


def _identifiers(node):
    return {value.casefold() for name in ("hardware_ids", "compatible_ids") for value in _ids(node, name)}


def _node_table(nodes):
    if not isinstance(nodes, Mapping) or not nodes or len(nodes) > 256:
        raise InputTargetsError("手柄设备树信息不完整，请重新检测。")
    table = {}
    for key, node in nodes.items():
        if (not isinstance(key, str) or not isinstance(node, Mapping)
                or not isinstance(node.get("instance_id"), str)
                or node["instance_id"].casefold() != key.casefold() or key.casefold() in table):
            raise InputTargetsError("手柄设备树存在重复或不一致的节点。")
        for name in ("parent", "service", "class_guid"):
            if not isinstance(node.get(name), str):
                raise InputTargetsError("手柄设备树信息不完整，请重新检测。")
        table[key.casefold()] = node
    return table


def _ancestors(device_id, table):
    result, seen = [], set()
    while device_id:
        key = device_id.casefold()
        if key in seen or len(result) >= 32:
            raise InputTargetsError("手柄设备树异常，未选择热切换设备。")
        seen.add(key)
        node = table.get(key)
        if node is None or node.get("present", True) is not True:
            raise InputTargetsError("所选手柄已断开或设备树读取不完整，请重新检测。")
        service = node["service"].casefold()
        if service in _VIRTUAL_SERVICES or key.startswith(("root\\vigem", "root\\vjoy", "root\\usbip")):
            raise InputTargetsError("热切换不能选择虚拟手柄。")
        result.append(node)
        device_id = node["parent"]
    return result


def _exclusive_chain(branch):
    """Each parent must have exactly the preceding child; the HID is a leaf."""
    for index, node in enumerate(branch):
        children = _ids(node, "children")
        expected = () if index == 0 else (branch[index - 1]["instance_id"].casefold(),)
        if tuple(child.casefold() for child in children) != expected:
            raise UnsupportedInputTargets("该输入分支还包含其他功能或手柄，无法单独热切换所选手柄。")
        if node["service"].casefold() in _UNSAFE_SERVICES:
            raise UnsupportedInputTargets("热切换不能重启键盘或鼠标输入分支。")
        if index and branch[index - 1]["parent"].casefold() != node["instance_id"].casefold():
            raise InputTargetsError("手柄设备父子关系已变化，请重新检测。")


def classify_input_targets(selected, nodes):
    """Pure validation; it cannot choose a hub, radio or caller-provided root."""
    device_id = _selection(selected)
    table = _node_table(nodes)
    chain = _ancestors(device_id, table)
    leaf = chain[0]
    if leaf["class_guid"].casefold() != HID_CLASS or not (_identifiers(leaf) & _GAME_IDS):
        raise UnsupportedInputTargets("所选 HID 节点不是已确认的游戏手柄输入。")
    apex = _APEX_INPUT.fullmatch(device_id)
    if apex:
        if len(chain) < 4:
            raise InputTargetsError("八爪鱼输入父节点不完整，请重新检测。")
        group = re.escape(apex[1])
        if (not re.fullmatch(r"USB\\VID_37D7&PID_2502&IG_" + group + r"\\[^\\]+", chain[1]["instance_id"], re.I)
                or not re.fullmatch(r"USB\\VID_37D7&PID_2502&MI_00\\[^\\]+", chain[2]["instance_id"], re.I)
                or not re.fullmatch(r"USB\\VID_37D7&PID_2502\\[^\\]+", chain[3]["instance_id"], re.I)):
            raise UnsupportedInputTargets("八爪鱼输入分支与已验证的接收器/USB 布局不一致。")
        if (chain[1]["service"].casefold() != "hidusb" or chain[1]["class_guid"].casefold() != HID_CLASS
                or chain[2]["service"].casefold() != "xusb22" or chain[2]["class_guid"].casefold() != XUSB_CLASS
                or "usb\\ms_comp_xusb10" not in _identifiers(chain[2])
                or "usb\\class_03" not in _identifiers(chain[1])
                or chain[3]["service"].casefold() != "usbccgp"):
            raise UnsupportedInputTargets("八爪鱼输入驱动未能确认，未选择重启设备。")
        branch = chain[:3]
        transport = "usb"
        if isinstance(selected, ControllerConnection):
            if selected.kind == "bluetooth":
                raise InputTargetsError("手柄连接方式已改变，请重新检测。")
            transport = selected.kind if selected.kind in ("usb", "receiver") else "usb"
    elif len(chain) >= 2 and _BT_PROFILE.fullmatch(chain[1]["instance_id"]):
        profile = chain[1]
        first_pair, parent_pair = _VID_PID.search(device_id), _VID_PID.search(profile["instance_id"])
        if (profile["service"].casefold() != "hidbth" or profile["class_guid"].casefold() != HID_CLASS
                or "bthenum\\" + BT_HID_SERVICE not in _identifiers(profile)
                or not first_pair or not parent_pair
                or tuple(v.casefold() for v in first_pair.groups()) != tuple(v.casefold() for v in parent_pair.groups())):
            raise UnsupportedInputTargets("蓝牙 HID 服务未能与所选手柄唯一对应。")
        if isinstance(selected, ControllerConnection) and selected.kind not in ("bluetooth", "unknown"):
            raise InputTargetsError("手柄连接方式已改变，请重新检测。")
        branch, transport = chain[:2], "bluetooth"
    else:
        raise UnsupportedInputTargets("此连接的输入设备布局尚不支持自动热切换；未重启任何设备。")
    _exclusive_chain(branch)
    if isinstance(selected, ControllerConnection):
        pair = _VID_PID.search(device_id)
        for expected, actual in zip((selected.vendor_id, selected.product_id), pair.groups() if pair else (None, None)):
            if expected is not None and (actual is None or type(expected) is not int or expected != int(actual, 16)):
                raise InputTargetsError("手柄设备身份已改变，请重新检测。")
    targets = tuple(node["instance_id"] for node in branch)
    return InputTargets(targets[0], targets[-1], targets, targets, transport)


def validate_input_anchor(targets, anchor):
    """Validate persisted ancestry; input descendants are deliberately excluded."""
    if (not isinstance(anchor, dict) or set(anchor) != {"format", "root_id", "physical_root_id", "hub_id", "chain"}
            or type(anchor["format"]) is not int or anchor["format"] != 1
            or not isinstance(anchor["root_id"], str) or anchor["root_id"].casefold() != targets.restart_id.casefold()
            or not isinstance(anchor["physical_root_id"], str) or not isinstance(anchor["hub_id"], str)
            or not isinstance(anchor["chain"], list) or not 1 <= len(anchor["chain"]) <= 32):
        raise InputTargetsError("手柄物理连接恢复记录无效。")
    seen = set()
    for row in anchor["chain"]:
        if (not isinstance(row, dict) or set(row) != {"device_id", "fingerprint"}
                or not isinstance(row["device_id"], str) or not 1 <= len(row["device_id"]) <= 1024
                or any(ord(c) < 32 for c in row["device_id"])
                or row["device_id"].casefold() in seen
                or not isinstance(row["fingerprint"], str) or not re.fullmatch("[0-9a-f]{64}", row["fingerprint"])):
            raise InputTargetsError("手柄物理连接恢复记录不完整。")
        seen.add(row["device_id"].casefold())
    chain = anchor["chain"]
    if chain[0]["device_id"].casefold() != targets.restart_id.casefold():
        raise InputTargetsError("手柄输入根节点已改变。")
    if targets.transport in ("usb", "receiver"):
        if (len(chain) < 3 or chain[1]["device_id"].casefold() != anchor["physical_root_id"].casefold()
                or chain[2]["device_id"].casefold() != anchor["hub_id"].casefold()
                or not re.fullmatch(r"USB\\VID_37D7&PID_2502\\[^\\]+", anchor["physical_root_id"], re.I)
                or not anchor["hub_id"].casefold().startswith("usb\\")):
            raise InputTargetsError("手柄物理 USB 路径无法确认。")
    elif anchor["physical_root_id"] or anchor["hub_id"]:
        raise InputTargetsError("蓝牙恢复记录不能包含 USB 重连范围。")
    return anchor


def _anchor_equal(left, right):
    def canonical(value):
        return (value["root_id"].casefold(), value["physical_root_id"].casefold(), value["hub_id"].casefold(),
                tuple((row["device_id"].casefold(), row["fingerprint"]) for row in value["chain"]))
    return canonical(left) == canonical(right)


def capture_input_anchor(targets, nodes=None):
    """Capture the exact input root and all physical ancestors, without writes."""
    selected = ControllerConnection(kind=targets.transport, device_id=targets.device_id)
    nodes = read_input_nodes(selected) if nodes is None else nodes
    current = classify_input_targets(selected, nodes)
    if (current.restart_id.casefold() != targets.restart_id.casefold()
            or tuple(v.casefold() for v in current.device_ids) != tuple(v.casefold() for v in targets.device_ids)):
        raise InputTargetsError("捕获物理连接前手柄输入分支已改变。")
    chain = _ancestors(targets.restart_id, _node_table(nodes))
    fingerprints = []
    for item in chain:
        values = [item["instance_id"].casefold(), item["service"].casefold(), item["class_guid"].casefold(),
                  sorted(v.casefold() for v in _ids(item, "hardware_ids")),
                  sorted(v.casefold() for v in _ids(item, "compatible_ids"))]
        digest = hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        fingerprints.append(dict(device_id=item["instance_id"], fingerprint=digest))
    usb = targets.transport in ("usb", "receiver")
    if usb and (len(chain) < 3 or chain[2]["service"].casefold() not in ("usbhub", "usbhub3")):
        raise InputTargetsError("无法确认手柄直接连接的物理 USB 集线器。")
    return validate_input_anchor(targets, dict(format=1, root_id=targets.restart_id,
        physical_root_id=chain[1]["instance_id"] if usb else "", hub_id=chain[2]["instance_id"] if usb else "",
        chain=fingerprints))


def classify_current_input(targets, anchor, nodes):
    """Accept an IG renumbering only under the captured exact physical branch."""
    validate_input_anchor(targets, anchor)
    table = _node_table(nodes)
    current = targets.restart_id
    for _ in range(2 if targets.transport in ("usb", "receiver") else 1):
        node = table.get(current.casefold())
        if node is None or len(_ids(node, "children")) != 1:
            raise InputTargetsError("原手柄输入分支尚未唯一恢复。")
        current = _ids(node, "children")[0]
    if targets.transport == "bluetooth" and current.casefold() != targets.device_id.casefold():
        raise UnsupportedInputTargets("蓝牙输入实例已改变，未自动改选手柄。")
    updated = classify_input_targets(ControllerConnection(kind=targets.transport, device_id=current), nodes)
    fresh = capture_input_anchor(updated, nodes)
    if not _anchor_equal(anchor, fresh):
        raise InputTargetsError("重新连接的输入不属于原来的物理手柄路径。")
    return updated


class _NativeTree:
    """Bounded cfgmgr32 sampling; no PnP state-changing imports or calls."""
    def __init__(self):
        if sys.platform != "win32":
            raise InputTargetsError("自动热切换的设备检测仅支持 Windows。")
        self.cfg = C.WinDLL("cfgmgr32.dll")
        uint = C.c_uint32
        for name, args in (
            ("CM_Locate_DevNodeW", [C.POINTER(uint), C.c_wchar_p, uint]),
            ("CM_Get_Device_IDW", [uint, C.c_wchar_p, uint, uint]),
            ("CM_Get_Parent", [C.POINTER(uint), uint, uint]),
            ("CM_Get_Child", [C.POINTER(uint), uint, uint]),
            ("CM_Get_Sibling", [C.POINTER(uint), uint, uint]),
            ("CM_Get_DevNode_Registry_PropertyW", [uint, uint, C.POINTER(uint), C.c_void_p, C.POINTER(uint), uint]),
        ):
            function = getattr(self.cfg, name)
            function.argtypes, function.restype = args, uint

    def name(self, node):
        value = C.create_unicode_buffer(1024)
        if self.cfg.CM_Get_Device_IDW(node, value, len(value), 0):
            raise InputTargetsError("所选手柄的设备节点已变化，请重新检测。")
        return value.value

    def prop(self, node, key, *, multi=False):
        size, kind = C.c_uint32(65536), C.c_uint32()
        buffer = C.create_string_buffer(size.value)
        code = self.cfg.CM_Get_DevNode_Registry_PropertyW(node, key, C.byref(kind), buffer, C.byref(size), 0)
        if code == 0x25:  # CR_NO_SUCH_VALUE: an optional property is absent.
            return [] if multi else ""
        if code or size.value > len(buffer) or size.value % 2 or kind.value != (7 if multi else 1):
            raise InputTargetsError("无法完整读取手柄设备属性，请重新检测。")
        value = buffer.raw[:size.value].decode("utf-16-le", errors="strict").rstrip("\0")
        return value.split("\0") if multi and value else ([] if multi else value)

    def related(self, node, method):
        related = C.c_uint32()
        code = getattr(self.cfg, method)(C.byref(related), node, 0)
        if code == 0x0d:  # CR_NO_SUCH_DEVNODE: no parent/child/sibling.
            return None
        if code:
            raise InputTargetsError("手柄设备树读取中断，请重新检测。")
        return related.value

    def children(self, node):
        children, seen = [], set()
        child = self.related(node, "CM_Get_Child")
        while child is not None:
            if child in seen or len(children) >= 32:
                raise InputTargetsError("输入分支过大或设备树异常，未选择重启设备。")
            seen.add(child)
            children.append(self.name(child))
            child = self.related(child, "CM_Get_Sibling")
        return children

    def read(self, device_id):
        node = C.c_uint32()
        if self.cfg.CM_Locate_DevNodeW(C.byref(node), device_id, 0):
            raise InputTargetsError("所选手柄已断开，请重新检测。")
        table, seen, current = {}, set(), node.value
        # Sample ancestors through the root, so a matching VID under a virtual
        # bus cannot pass as physical. Only the first three nodes need children.
        while current is not None:
            if current in seen or len(table) >= 32:
                raise InputTargetsError("手柄设备树异常，未选择热切换设备。")
            seen.add(current)
            name = self.name(current)
            parent = self.related(current, "CM_Get_Parent")
            table[name] = dict(instance_id=name, parent=self.name(parent) if parent is not None else "",
                               children=self.children(current) if len(table) < 3 else [],
                               service=self.prop(current, 5), class_guid=self.prop(current, 9),
                               hardware_ids=self.prop(current, 2, multi=True),
                               compatible_ids=self.prop(current, 3, multi=True), present=True)
            current = parent
        return table

    def read_current(self, targets):
        node = C.c_uint32()
        if self.cfg.CM_Locate_DevNodeW(C.byref(node), targets.restart_id, 0):
            raise InputTargetsError("原手柄输入根节点尚未恢复。")
        current = node.value
        for _ in range(2 if targets.transport in ("usb", "receiver") else 1):
            children = self.children(current)
            if len(children) != 1:
                raise InputTargetsError("原手柄输入分支尚未唯一恢复。")
            child = C.c_uint32()
            if self.cfg.CM_Locate_DevNodeW(C.byref(child), children[0], 0) or self.related(child.value, "CM_Get_Parent") != current:
                raise InputTargetsError("手柄输入父子关系正在变化。")
            current = child.value
        return self.read(self.name(current))


def read_input_nodes(selected):
    """Read the selected instance and its ancestors for diagnostics/classifying."""
    return _NativeTree().read(_selection(selected))


def resolve_input_targets(selected):
    """Return targets for this exact live HID input, without changing anything."""
    return classify_input_targets(selected, read_input_nodes(selected))


def resolve_current_input(targets, anchor):
    """Read the sole current input beneath an already captured physical root."""
    validate_input_anchor(targets, anchor)
    return classify_current_input(targets, anchor, _NativeTree().read_current(targets))
