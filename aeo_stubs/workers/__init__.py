"""Minimal workers shim for local dev."""

class Context:
    pass

class Request:
    def __init__(self, *args, **kwargs):
        pass

class WorkerEntrypoint:
    def __init__(self, app):
        self.app = app

class DurableObjectNamespace:
    pass

class ScheduledState:
    pass

def get(env, name):
    return getattr(env, name, None)
