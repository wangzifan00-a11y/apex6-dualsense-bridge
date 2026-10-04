"""Read-only receiver identity checks on a separate HID handle."""
import threading

from dsbridge.controllers.flydigi.apex6.hid import ReceiverError, ReceiverHID
from dsbridge.controllers.flydigi.apex6.protocol import ReceiverProtocol


class ReceiverMonitor:
    def __init__(self, session):
        self.path = session.transport.path
        self.uid, self.slot = session.identity["uid"], session.slot
        self.cancel = threading.Event()
        self.thread = None
        self.error = None
        self.checks = 0

    def start(self):
        self.thread = threading.Thread(target=self._run, name="Receiver identity checks", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with ReceiverHID(self.path) as transport:
                protocol = ReceiverProtocol(transport, cancel=self.cancel)
                while not self.cancel.wait(1):
                    if protocol.identity()["uid"] != self.uid:
                        raise ReceiverError("接收器连接的手柄已更换，转换已停止")
                    if self.cancel.is_set():
                        return
                    if protocol.slot() != self.slot:
                        raise ReceiverError("手柄配置槽已更换，请重新启动转换")
                    self.checks += 1
        except Exception as exc:
            if not self.cancel.is_set():
                self.error = exc

    def stop(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(3)
            if self.thread.is_alive():
                raise ReceiverError("接收器身份查询仍在停止")
            self.thread = None
