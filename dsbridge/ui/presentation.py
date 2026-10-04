"""Pure UI formatting; the view has no brand-specific device knowledge."""


def feedback_text(state, rumble, metadata):
    level = "左 {:.0%} / 右 {:.0%}".format(*rumble)
    meter = "输入：按键 {:04X} · LT {} · RT {}    反馈：{}".format(
        state["buttons"], state["left_trigger"], state["right_trigger"], level)
    if not metadata:
        return meter, "", False
    native = ("已接通" if metadata.get("native_haptics_nonzero_frames") else
              "音轨已打开，等待触觉" if metadata.get("native_haptics_frames") else "等待游戏输出")
    rumble_text = ("已接通" if metadata.get("rumble_nonzero_commands") else
                   "已连接，等待震动" if metadata.get("rumble_commands") else "等待游戏输出")
    feedback = "DS 触觉：" + native + "    普通震动：" + rumble_text
    output = metadata.get("output")
    if output:
        meter = "马达输出：" + " · ".join(f"{label} {value:.0%}" for label, value in
                                        zip(output["labels"], output["values"]))
    triggers = metadata.get("triggers")
    if triggers:
        feedback += "    DS 扳机：" + ("已接收效果" if any(triggers["nonzero_effects"]) else "等待游戏效果")
    protection = metadata.get("audio_protection") or {}
    return meter, feedback, bool(protection.get("active"))
