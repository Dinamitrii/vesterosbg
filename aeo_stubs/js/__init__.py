"""Minimal js shim for local dev."""

window = None
document = None
navigator = type("Navigator", (), {"userAgent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})()
localStorage = type("LocalStorage", (), {"getItem": lambda self, k: None, "setItem": lambda self, k, v: None, "removeItem": lambda self, k: None})()
sessionStorage = type("SessionStorage", (), {"getItem": lambda self, k: None, "setItem": lambda self, k, v: None, "removeItem": lambda self, k: None})()
indexedDB = None
fetch = None
WebSocket = None
__log__ = []
def __log(msg):
    __log__.__dict__.setdefault("_messages", []).append(msg)
    print(f"[js] {msg}")

def _methods():
    return {}
