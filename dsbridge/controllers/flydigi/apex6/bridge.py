"""Compose the APEX 6 output plugin with the shared Sony bridge."""
from dsbridge.core.managed_bridge import ManagedFeedbackBridge
from dsbridge.controllers.flydigi.apex6.output import Apex6Output
from dsbridge.controllers.flydigi.apex6.session import Apex6Session
from dsbridge.controllers.flydigi.apex6.monitor import ReceiverMonitor
from dsbridge.controllers.flydigi.apex6.recovery import ReceiverInputLease
from dsbridge.virtual.dualsense.runtime import LocalViiper
from dsbridge.virtual.dualsense.feedback import decode_feedback
from dsbridge.platform.windows.isolation import verify_isolation
from dsbridge.runtime.paths import as_paths


class ReceiverFeedback(ManagedFeedbackBridge):
    def __init__(self, base, config, *, session_factory=Apex6Session, viiper_factory=LocalViiper,
                 monitor_factory=ReceiverMonitor, isolation_factory=ReceiverInputLease):
        self.paths = as_paths(base)
        self.base = self.paths.state
        output = Apex6Output(self.base, config, session_factory=session_factory, monitor_factory=monitor_factory)
        lease = isolation_factory(self.paths) if isolation_factory else None
        super().__init__(output, lambda sink: viiper_factory(self.paths, config, feedback_sink=sink),
                         decoder=decode_feedback, isolation=lease, verify=lambda: verify_isolation(self.paths))

    # Compatibility for existing diagnostic scripts. New adapters use .output.
    def __getattr__(self, name):
        return getattr(self.output, name)

    @property
    def _failure(self):
        return self.output._failure

    @_failure.setter
    def _failure(self, value):
        self.output._failure = value
