"""
aeo-mcp-server.py — MCP (Model Context Protocol) server for the vesterosbg project.

This module exposes the vesterosbg project to AI agents (MCP clients) through
a small, standards-compliant HTTP interface. It provides three entry points:

  - GET  /mcp                          — capability / server discovery
  - POST /mcp                          — JSON-RPC 2.0 API (tools/call, tools/list,
                                          resources/read, prompts/get, ...)
  - GET  /mcp/stream                   — SSE stream for long-lived tool calls

It also works as a Flask blueprint (`McpView`) so it can be mounted on an
existing Flask/Werkzeug app, or as a standalone factory (`mcp_server()`).

The MCP endpoint lives next to the normal website API, so an AI agent can read
the site configuration / database schema / project status and then drive the
site (or a human operator) through the same values it sees on the page.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

try:
    from flask import Blueprint, Response, current_app, jsonify, request
    _FLASK_AVAILABLE = True
except ImportError:  # Pyodide/WASM runtime ships without Flask — MCP is optional.
    Blueprint = Response = current_app = jsonify = request = None  # type: ignore[assignment]
    _FLASK_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP protocol constants
# ---------------------------------------------------------------------------
MCP_PROTOCOL_VERSION = "2024-11-05"

MCP_METHOD_TOOLS_CALL = "tools/call"
MCP_METHOD_TOOLS_LIST = "tools/list"
MCP_METHOD_TOOLS_REGISTER = "tools/register"
MCP_METHOD_RESOURCES_LIST = "resources/list"
MCP_METHOD_RESOURCES_READ = "resources/read"
MCP_METHOD_RESOURCES_REGISTER = "resources/register"
MCP_METHOD_PROMPTS_LIST = "prompts/list"
MCP_METHOD_PROMPTS_GET = "prompts/get"

MCP_RESPONSE_OK = "ok"
MCP_RESPONSE_ERROR = "error"

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 error codes
# ---------------------------------------------------------------------------
JSON_RPC_PARSE_ERROR = -32700
JSON_RPC_INVALID_REQUEST = -32600
JSON_RPC_METHOD_NOT_FOUND = -32601
JSON_RPC_INVALID_PARAMS = -32602
JSON_RPC_INTERNAL_ERROR = -32603

MCP_ERROR_INVALID_PARAMS = -32000
MCP_ERROR_NOT_FOUND = -32001
JSON_RPC_NOT_FOUND = -32001
MCP_ERROR_TIMEOUT = -32003
MCP_ERROR_INTERNAL = -32004

# Registry: tool name -> {name, description, inputSchema, handler}
MCP_TOOLS: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Helpers / registry
# ---------------------------------------------------------------------------
def register_tool(name: str, description: str, input_schema: dict, handler: Callable):
    """Decorator to register an MCP tool."""

    def wrapper(fn: Callable):
        MCP_TOOLS[name] = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "handler": handler,
        }
        # ``fn`` is the decorated function (returned for compatibility, not used).
        return fn

    return wrapper


def _jsonrpc_response(id: Any, result: Any = None, error: Optional[dict] = None) -> dict:
    response = {"jsonrpc": "2.0", "id": id}
    if error is not None:
        response["error"] = error
    else:
        response["result"] = result
    return response


def _jsonrpc_error(code: int, message: str, data: Optional[dict] = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return error


def _invoke_tool(name: str, params: dict) -> Any:
    tool = MCP_TOOLS.get(name)
    if tool is None:
        raise ValueError(f"Unknown tool '{name}'")
    return tool["handler"](params)


# ---------------------------------------------------------------------------
# Flask blueprint: the MCP HTTP API
# ---------------------------------------------------------------------------
if _FLASK_AVAILABLE:
    mcp_bp = Blueprint("mcp", __name__, url_prefix="/mcp")

    def _route(method, path):
        return getattr(mcp_bp, method)(path)
else:  # Pyodide/WASM runtime ships without Flask — skip blueprint registration.
    mcp_bp = None

    def _route(method, path):  # type: ignore[no-redef]
        def decorator(fn):
            return fn
        return decorator


def _get_db():
    """Return the Flask app's SQLite connection factory, if one was registered."""
    return current_app.extensions.get("mcp_db") if current_app else None


def _settings_dict():
    """Read the project settings table (vendor-specific, project-local)."""
    conn = _get_db()
    if conn is None:
        return {}
    try:
        return {row["key"]: row["value"] for row in conn.execute(
            "SELECT key, value FROM settings")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@_route("get", "/")
def mcp_capabilities() -> Response:
    """Capability discovery: what this MCP server offers."""
    return jsonify(
        _jsonrpc_response(
            "server-capabilities",
            result={
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False},
                    "prompts": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "vesterosbg-mcp",
                    "version": "0.1.0",
                    "title": "Vesteros BG — website, SEO & AEO",
                },
            },
        )
    )


@_route("post", "/")
def mcp_dispatch() -> Response:
    """Dispatch a JSON-RPC 2.0 request (tools/call, tools/list, ...)."""
    body = request.get_json(silent=True, force=True)
    if body is None:
        return jsonify(
            _jsonrpc_response(None, error=_jsonrpc_error(JSON_RPC_PARSE_ERROR, "Invalid JSON"))
        ), 400

    if isinstance(body, list):
        return _handle_batch(body)

    return _handle_single(body)


@_route("get", "/stream")
@_route("post", "/stream")
def mcp_stream() -> Response:
    """Server-Sent Events stream for long-running tool calls."""
    if request.method == "GET":
        # Client asks for the stream capability.
        return _jsonrpc_response("stream-capability", result={"enabled": True})

    # POST /mcp/stream starts a streaming tool call (SSELite).
    data = request.get_json(silent=True, force=True) or {}
    tool_name = data.get("tool")
    tool_params = data.get("params", {})

    def generate():
        # Send the tool invocation start event.
        yield f"event: start\ndata: {json.dumps({'tool': tool_name, 'status': 'started'})}\n\n"
        try:
            result = _invoke_tool(tool_name, tool_params)
            yield f"event: result\ndata: {json.dumps({'status': 'done', 'result': result})}\n\n"
        except Exception as exc:  # noqa: BLE001
            yield f"event: error\ndata: {json.dumps({'status': 'error', 'error': str(exc)})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------
def _handle_single(request_obj: dict):
    method = request_obj.get("method")
    params = request_obj.get("params", {}) or {}
    request_id = request_obj.get("id")

    if method == MCP_METHOD_TOOLS_LIST:
        return _handle_tools_list(request_id)
    if method == MCP_METHOD_TOOLS_CALL:
        return _handle_tools_call(request_id, params)
    if method == MCP_METHOD_RESOURCES_LIST:
        return _handle_resources_list(request_id)
    if method == MCP_METHOD_RESOURCES_READ:
        return _handle_resources_read(request_id, params)
    if method == MCP_METHOD_PROMPTS_LIST:
        return _handle_prompts_list(request_id)
    if method == MCP_METHOD_PROMPTS_GET:
        return _handle_prompts_get(request_id, params)

    res = jsonify(
        _jsonrpc_response(request_id, error=_jsonrpc_error(JSON_RPC_METHOD_NOT_FOUND, f"Method '{method}' not found"))
    )
    res.status_code = 404
    return res


def _handle_batch(requests: list[dict]) -> Response:
    results = []
    for req in requests:
        try:
            response = _handle_single(req)
            results.append(response.get_json(silent=True) or {})
        except Exception as exc:  # noqa: BLE001
            results.append(
                _jsonrpc_response(req.get("id"), error=_jsonrpc_error(JSON_RPC_INTERNAL_ERROR, str(exc)))
            )
    return jsonify(results)


def _handle_tools_list(id: Any):
    tools = []
    for tool in MCP_TOOLS.values():
        item = dict(tool)
        item.pop("handler", None)
        tools.append(item)
    return jsonify(_jsonrpc_response(id, result={"tools": tools}))


def _handle_tools_call(id: Any, params: dict):
    name = params.get("name") if isinstance(params, dict) else None
    arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
    if not name:
        return jsonify(_jsonrpc_response(id, error=_jsonrpc_error(JSON_RPC_INVALID_PARAMS, "Missing 'name'")))
    try:
        result = _invoke_tool(name, arguments)
        resp = jsonify(_jsonrpc_response(id, result=result))
    except Exception as exc:  # noqa: BLE001
        resp = jsonify(_jsonrpc_response(id, error=_jsonrpc_error(JSON_RPC_INTERNAL_ERROR, str(exc))))
        resp.status_code = 500
    return resp


def _handle_resources_list(id: Any):
    return jsonify(
        _jsonrpc_response(id, result={
            "resources": [
                {
                    "uri": "vesterosbg://settings/config",
                    "name": "Project settings",
                    "description": "Company name, legal form, EIK, address, phone",
                    "mimeType": "application/json",
                },
                {
                    "uri": "vesterosbg://migrations/schema",
                    "name": "Database schema",
                    "description": "Last applied schema migration",
                    "mimeType": "application/sql",
                },
            ]
        })
    )


def _handle_resources_read(id: Any, params: dict):
    uri = (params or {}).get("uri")
    if uri is None:
        return jsonify(_jsonrpc_response(id, error=_jsonrpc_error(JSON_RPC_INVALID_PARAMS, "Missing 'uri'")))

    if uri == "vesterosbg://settings/config":
        return jsonify(_jsonrpc_response(id, result={
            "contents": [{
                "uri": uri,
                "text": json.dumps({"status": "ready", "mcp": "enabled"}, indent=2),
            }],
        }))

    if uri == "vesterosbg://migrations/schema":
        from pathlib import Path
        migration_path = Path(__file__).parent / "migrations" / "0004_id_reg_and_admin_rights.sql"
        if migration_path.exists():
            return jsonify(_jsonrpc_response(id, result={
                "contents": [{
                    "uri": uri,
                    "text": migration_path.read_text(encoding="utf-8"),
                }],
            }))
        return jsonify(_jsonrpc_response(id, error=_jsonrpc_error(JSON_RPC_NOT_FOUND, "Migration file not found")))

    return jsonify(_jsonrpc_response(id, error=_jsonrpc_error(JSON_RPC_NOT_FOUND, f"Resource '{uri}' not found")))


def _handle_prompts_list(id: Any):
    return jsonify(_jsonrpc_response(id, result={
        "prompts": [
            {
                "name": "system-status",
                "description": "Get current project status overview",
                "arguments": [],
            },
            {
                "name": "db-info",
                "description": "Get database information",
                "arguments": [{"name": "db_name", "description": "Database name", "required": False}],
            },
        ],
    }))


def _handle_prompts_get(id: Any, params: dict):
    name = (params or {}).get("name")
    if name == "system-status":
        return jsonify(_jsonrpc_response(id, result={
            "description": "Current project status",
            "messages": [{
                "role": "assistant",
                "content": {"type": "text", "text": "Vesteros BG site is ready and served via Flask + Cloudflare Python Worker."},
            }],
        }))
    if name == "db-info":
        db_name = (params or {}).get("db_name", "vesterosbg-db")
        return jsonify(_jsonrpc_response(id, result={
            "description": f"Information about database '{db_name}'",
            "messages": [{
                "role": "assistant",
                "content": {"type": "text", "text": f"Database: {db_name} (SQLite, connected via Flask)"},
            }],
        }))
    return jsonify(_jsonrpc_response(id, error=_jsonrpc_error(JSON_RPC_NOT_FOUND, f"Prompt '{name}' not found")))


# ---------------------------------------------------------------------------
# MCP tools (registered with register_tool)
# ---------------------------------------------------------------------------
@register_tool(
    "system_status",
    "Get a short project status overview (served via MCP, no arguments).",
    {},
    lambda params: {
        "status": "ready",
        "mcp": "enabled",
        "site_url": current_app.config.get("SITE_URL", ""),
        "host": current_app.config.get("HOST", "localhost"),
        "workers": current_app.config.get("WORKERS", "flask"),
    },
)
def tool_system_status(params: dict):
    return {}


@register_tool(
    "get_settings",
    "Read the project settings table (company name, EIK, address, phone, ...).",
    {"db": {"type": "string", "description": "Optional database name override"}},
    lambda params: {
        "settings": _settings_dict() or {},
        "db_name": (params or {}).get("db", "vesterosbg-db"),
    },
)
def tool_get_settings(params: dict):
    return {}


@register_tool(
    "get_site_config",
    "Read the site configuration (SITE_URL, WHATSAPP_PHONE, SECRET_KEY).",
    {"keys": {"type": "array", "description": "Optional list of config keys to return", "items": {"type": "string"}}},
    lambda params: {
        "config": {
            k: v for k, v in current_app.config.items()
            if k in {*[params.get("keys", [])], "SITE_URL", "WHATSAPP_PHONE", "SECRET_KEY", "HOST", "PORT"}
        }
    },
)
def tool_get_site_config(params: dict):
    return {}


# ---------------------------------------------------------------------------
# Full-stack helpers (Flask / Cloudflare Worker)
# ---------------------------------------------------------------------------
class McpView:
    """Flask view that mounts the MCP blueprint."""

    def __init__(self, app=None):
        self.blueprint = mcp_bp
        if app is not None:
            self.register(app)

    def register(self, app):
        if mcp_bp is None:
            return self
        app.register_blueprint(mcp_bp)
        # Expose the DB factory for the MCP tools (avoids a circular import).
        app.extensions["mcp_db"] = getattr(app, "db", None)
        return self


McpApp = McpView  # FastAPI-friendly alias


def mcp_server(app=None):
    """Factory: build a Flask app wired with the MCP endpoints."""
    from flask import Flask

    app = Flask(__name__)
    McpView(app=app)
    return app


__all__ = [
    "mcp_bp",
    "McpView",
    "McpApp",
    "mcp_server",
    "register_tool",
    "MCP_TOOLS",
]
