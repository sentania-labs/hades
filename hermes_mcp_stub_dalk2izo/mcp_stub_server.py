#!/usr/bin/env python3
"""Minimal MCP stdio server with one tool for testing."""
import json, sys


def _respond(request_id, result=None, error=None):
    obj = {"jsonrpc": "2.0", "id": request_id}
    if result is not None:
        obj["result"] = result
    if error is not None:
        obj["error"] = error
    print(json.dumps(obj))


def main():
    while True:
        line = sys.stdin.readline().rstrip("\n")
        if not line:
            break
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = req.get("id")
        method = req.get("method", "")
        if method == "initialize":
            _respond(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "hermes-test", "version": "0.0.1"},
            })
        elif method == "tools/list":
            _respond(rid, {
                "tools": [{
                    "name": "echo_tool",
                    "description": "Echo back input text",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string"}
                        },
                        "required": ["message"],
                    },
                }],
            })
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            if name == "echo_tool":
                msg = arguments.get("message", "")
                _respond(rid, {
                    "content": [{"type": "text", "text": "echo_tool received: " + str(msg)}],
                })
            else:
                _respond(rid, error={
                    "code": -32601,
                    "message": "Unknown tool: " + name,
                })
        elif method == "notifications/initialized":
            pass
        else:
            _respond(rid, error={
                "code": -32601,
                "message": "Unknown: " + method,
            })


if __name__ == "__main__":
    main()
