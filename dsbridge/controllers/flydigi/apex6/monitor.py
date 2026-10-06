"""Read-only receiver identity checks on a separate HID handle."""
import threading

from dsbridge.controllers.flydigi.apex6.hid import ReceiverDisconnected, ReceiverError, ReceiverHID
from dsbridge.controllers.flydigi.apex6.protocol import ReceiverProtocol, _ReplyTimeout


class ReceiverMonitor:
    def __init__(self, session, *, transport_factory=ReceiverHID, protocol_factory=ReceiverProtocol, interval=0.5):
        self.path = session.transport.path
        self.uid, self.slot = session.identity["uid"], session.slot
        self.cancel = threading.Event()
        self.thread = None
        self.error = None
        self.checks = 0
        self.transport_factory, self.protocol_factory = transport_factory, protocol_factory
        self.interval = interval

    def start(self):
        self.thread = threading.Thread(target=self._run, name="Receiver identity checks", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with self.transport_factory(self.path) as transport:
                protocol = self.protocol_factory(transport, cancel=self.cancel)
                while not self.cancel.wait(self.interval):
                    # A USB receiver can remain enumerated after the pad powers
                    # off. Require new read-only replies, not cached HID input.
                    transport.flush_queue()
                    if protocol.identity()["uid"] != self.uid:
                        raise ReceiverError("接收器连接的手柄已更换，转换已停止")
                    if self.cancel.is_set():
                        return
                    if protocol.slot() != self.slot:
                        raise ReceiverError("手柄配置槽已更换，请重新启动转换")
                    self.checks += 1
        except Exception as exc:
            if not self.cancel.is_set():
                self.error = (ReceiverDisconnected("手柄连接或接收器通信已中断，正在停止本次桥接")
                              if isinstance(exc, _ReplyTimeout) else exc)

    def stop(self):
        self.cancel.set()
        if self.thread:
            self.thread.join(3)
            if self.thread.is_alive():
                raise ReceiverError("接收器身份查询仍在停止")
            self.thread = None
