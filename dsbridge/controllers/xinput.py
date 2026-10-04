"""Generic two-motor input adapter. It makes no brand/transport claim."""
import time
from dsbridge.core.modes import PROFILES
from dsbridge.core.ports import ControllerAdapter


def input_factory():
    from dsbridge.platform.windows.xinput import XInput
    return XInput()


def backend_factory(paths, config, profile):
    if profile.sony != "dualsense":
        raise ValueError("此版本仅支持 PS5 DualSense 桥接")
    from dsbridge.virtual.dualsense.runtime import LocalViiper
    return LocalViiper(paths, config)


def motor_test(paths, input_port, slot, notify):
    try:
        for values in ((.22, 0), (0, .22)):
            if input_port.set_rumble(slot, *values) is False:
                raise RuntimeError("手柄已断开，轻震测试失败")
            time.sleep(.25)
        if input_port.set_rumble(slot, 0, 0) is False:
            raise RuntimeError("手柄已断开，无法确认停振")
        notify("已发送左右各 250 ms 的轻震及停振指令。实际体感需人工确认。")
    finally:
        input_port.set_rumble(slot, 0, 0)


def adapter():
    return ControllerAdapter("xinput", "通用 XInput 双马达", "通用",
                             ("提供 XInput 输入和左右震动的手柄",), ("bluetooth", "usb", "receiver"),
                             ("input", "rumble.stereo"), input_factory, backend_factory, motor_test,
                             (PROFILES[1],))
