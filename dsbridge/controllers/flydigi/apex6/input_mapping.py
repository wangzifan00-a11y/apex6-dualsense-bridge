"""Model-specific input semantics, applied only to the virtual Sony stream."""
from collections.abc import Mapping

APEX6_PRO_MODEL = "flydigi.apex6pro"
VIEW_BUTTON = 0x0020


def unchanged(state):
    return state


def view_to_touchpad(state):
    """Consume View/Create and forward its held/released state as touchpad click."""
    if not isinstance(state, Mapping):
        raise ValueError("手柄输入必须是按键状态")
    buttons = state.get("buttons", 0)
    if isinstance(buttons, bool) or not isinstance(buttons, int) or not 0 <= buttons <= 0xFFFF:
        raise ValueError("手柄按键状态无效")
    return {**state, "buttons": buttons & ~VIEW_BUTTON,
            "touchpad_click": bool(buttons & VIEW_BUTTON)}


def mapping_for(connection):
    if (getattr(connection, "model", "") == APEX6_PRO_MODEL
            and connection.kind in ("bluetooth", "usb", "receiver")):
        return view_to_touchpad
    return unchanged
