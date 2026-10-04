from dsbridge.core.ports import BridgeProfile

# Stable IDs preserve existing receiver tests and session diagnostics; removed
# IDs must not be reassigned to another controller backend.
MODES = {1: "PS5 双马达桥接", 5: "PS5 接收器四马达桥接"}
MODE_IDS = {label: mode for mode, label in MODES.items()}
DETAILS = {
    1: "游戏识别为 PS5 DualSense。蓝牙和通用 USB 手柄将游戏原生 DS 反馈转为左右马达强度，波形细节会损失。",
    5: "接收器专用：DS 触觉送到握把，DS 扳机效果转为扳机震动。启动时自动隐藏实体输入；停止或退出后自动恢复普通输入。游戏识别为 Sony 手柄，扳机阻力为震动模拟。",
}

PROFILES = {
    1: BridgeProfile(1, MODES[1], DETAILS[1], "xinput"),
    5: BridgeProfile(5, MODES[5], DETAILS[5], "flydigi.apex6.receiver",
                     managed_output=True, unique_input=True, test_label="测试四个马达",
                     start_notice="正在自动隔离接收器输入；停止转换或退出后自动恢复。游戏需选择 Sony 手柄，并关闭该游戏的 Steam Input。",
                     stopped_notice="已停止转换，接收器普通输入已恢复。"),
}

