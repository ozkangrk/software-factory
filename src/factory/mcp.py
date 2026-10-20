"""factory mcp — ajan araçlarını stdio JSON-RPC 2.0 (MCP) olarak sunar.

Claude Code / Codex CLI ayarında:
    claude mcp add factory -- python -m factory.mcp --workspace /path/ws

Protokol: newline-delimited JSON-RPC. Yalnız initialize / tools/list /
tools/call desteklenir — bilinçli minimum (ADR-002).
"""
from __future__ import annotations

import argparse
import json
import sys

from .agent_tools import dispatch, tool_manifest
from .store import Store

PROTOCOL = "2024-11-05"


def _call(store: Store, req: dict) -> dict:
    rid = req.get("id")
    method = req.get("method", "")
    try:
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": PROTOCOL,
                "serverInfo": {"name": "factory", "version": "0.1.0"},
                "capabilities": {"tools": {}}}}
        if method == "notifications/initialized":
            return None                      # bildirim: yanıt yok
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"tools": tool_manifest()}}
        if method == "tools/call":
            params = req.get("params", {})
            res = dispatch(store, params.get("name", ""),
                           params.get("arguments", {}))
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": json.dumps(res, ensure_ascii=False)}],
                "isError": not res.get("ok", False)}}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"yöntem yok: {method}"}}
    except Exception as e:                   # asla çökme — hata dön
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32000, "message": str(e)}}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="factory-mcp")
    ap.add_argument("--workspace", required=True)
    args = ap.parse_args(argv)
    store = Store(f"{args.workspace}/registry.db")
    store._workspace = args.workspace
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "geçersiz JSON"}}
            print(json.dumps(out), flush=True)
            continue
        resp = _call(store, req)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
