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
        return self.start_cancellable(lambda: None)

    def start_cancellable(self, check):
        try:
            check()
            if self.isolation:
                start_isolation = getattr(self.isolation, "start_cancellable", None)
                start_isolation(check) if start_isolation else self.isolation.start()
            check()
            self.verify()
            check()
            start_output = getattr(self.output, "start_cancellable", None)
            start_output(check) if start_output else self.output.start()
            check()
            start_virtual = getattr(self.viiper, "start_cancellable", None)
            start_virtual(check) if start_virtual else self.viiper.start()
            check()
            self.output.arm()
        except Exception:
            try:
                self.stop()
            except Exception as cleanup:
                self.last_cleanup_error = "；".join(filter(None, (self.last_cleanup_error, str(cleanup))))
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
            error = self.error
            raise error if isinstance(error, Exception) else RuntimeError(str(error))
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
