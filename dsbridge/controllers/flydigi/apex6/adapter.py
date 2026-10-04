"""Explicitly opt in to the verified APEX 6 receiver protocol."""
from dsbridge.core.modes import PROFILES
from dsbridge.core.ports import ControllerAdapter
from dsbridge.controllers.xinput import input_factory


def backend_factory(paths, config, profile):
    from dsbridge.controllers.flydigi.apex6.bridge import ReceiverFeedback
    return ReceiverFeedback(paths, config)


def motor_test(paths, input_port, slot, notify):
    from dsbridge.controllers.flydigi.apex6.session import motor_test as test
    test(paths.state, notify=notify)
    notify("四路轻震发送完成，马达设置已核对恢复。实际位置请按体感确认。")


def adapter():
    return ControllerAdapter("flydigi.apex6.receiver", "八爪鱼 6 接收器四马达", "Flydigi",
                             ("APEX 6 / 八爪鱼 6 Pro（协议能力校验后启用）",), ("receiver",),
                             ("input", "rumble.stereo", "pcm.stereo", "trigger.vibration"),
                             input_factory, backend_factory, motor_test, (PROFILES[5],))
