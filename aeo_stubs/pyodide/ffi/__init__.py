"""Minimal pyodide.ffi shim for local dev."""
def create_proxy(*args, **kwargs):
    return args[0] if args else None

def to_js(*args, **kwargs):
    return list(args) if args else []
