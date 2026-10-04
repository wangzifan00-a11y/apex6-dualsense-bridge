"""Developer template. Does not discover or write any real device."""
from dsbridge.core.ports import ControllerAdapter, BridgeProfile


class ExampleInput:
    slots = ()  # Replace with verified device identifiers, never assume XInput 0.
    def label(self, slot):
        return "示例手柄 " + str(slot)
    def get_state(self, slot):
        return None
    def set_rumble(self, slot, left, right):
        raise NotImplementedError("Implement only after verifying the model protocol")
    def close(self):
        pass


def backend(paths, config, profile):
    from dsbridge.virtual.dualsense.runtime import LocalViiper
    return LocalViiper(paths, config)


def test_motors(paths, input_port, slot, notify):
    raise NotImplementedError("示例包尚未实现硬件输出")


def register(registry):
    profile = BridgeProfile("example.model.usb", "开发示例（未实现硬件）", "只演示扩展接口", "example.model")
    registry.register(ControllerAdapter("example.model", "扩展示例", "ExampleBrand", ("ExampleModel",),
                                        ("usb",), ("input", "rumble.stereo"),
                                        ExampleInput, backend, test_motors, (profile,)))
