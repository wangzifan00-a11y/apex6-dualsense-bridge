"""Compose a Sony source with a physical waveform sink and isolation lease."""


class ManagedFeedbackBridge:
    physical_output = True

    def __init__(self, output, virtual_factory, *, decoder, isolation=None, verify=lambda: None):
        self.output = output
        self.decoder = decoder
        self.isolation = isolation
        self.verify = verify
        self.viiper = virtual_factory(self.on_feedback)
        self.last_cleanup_error = None

    def on_feedback(self, kind, payload, at):
        feedback = self.decoder(kind, payload, at)
        if feedback is not None:
            self.output.accept(feedback)

    def start(self):
        try:
            if self.isolation:
                self.isolation.start()
            self.verify()
            self.output.start()
            self.viiper.start()
            self.output.arm()
        except Exception:
            self.stop()
            raise

    @property
    def error(self):
        return self.output.error or (self.isolation.error if self.isolation else None) or self.viiper.error

    def set_gain(self, gain):
        self.output.set_gain(gain)

    def update(self, state):
        self.output.update(state)
        self.viiper.update(state)

    def poll_feedback(self):
        if self.error:
            raise RuntimeError(str(self.error))
        return self.output.poll_feedback()

    def status(self):
        return {**self.viiper.status(), **self.output.status()}

    def stop(self):
        try:
            self.output.stop()  # Motor neutral/config restore before slow USB cleanup.
        finally:
            try:
                self.viiper.stop()
            finally:
                try:
                    if self.isolation:
                        self.isolation.stop()  # Physical input returns after virtual removal.
                finally:
                    self.last_cleanup_error = "；".join(filter(None, (
                        getattr(self.output, "last_cleanup_error", None),
                        getattr(self.viiper, "last_cleanup_error", None)))) or None
