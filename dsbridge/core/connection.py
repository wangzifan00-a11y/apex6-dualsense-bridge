"""Transport information supplied by an input adapter, independent of the OS."""
from dataclasses import asdict, dataclass

LABELS = {"bluetooth": "蓝牙连接", "receiver": "2.4G 接收器连接", "usb": "USB 有线连接",
          "unknown": "连接方式暂未识别", "disconnected": "手柄已断开", "virtual": "虚拟手柄"}


@dataclass(frozen=True)
class ControllerConnection:
    kind: str = "unknown"
    name: str = ""
    detail: str = ""
    device_id: str = ""
    vendor_id: int | None = None
    product_id: int | None = None
    profile: int | str | None = None

    @property
    def label(self):
        return LABELS.get(self.kind, LABELS["unknown"])

    def to_dict(self):
        return {**asdict(self), "label": self.label}
