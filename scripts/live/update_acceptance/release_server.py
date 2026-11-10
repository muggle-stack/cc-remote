#!/usr/bin/env python3
"""Private HTTPS release fixture. Trust its CA only inside disposable test containers."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import threading
import time
from urllib.parse import unquote, urlsplit


class Server(ThreadingHTTPServer):
    def __init__(self, args):
        self.folder = args.folder.resolve()
        self.lock = threading.Lock()
        self.repository = args.repository
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(args.cert, args.key)
        super().__init__(("0.0.0.0", args.port), Handler)

    def fault(self, kind, path=""):
        with self.lock:
            control = self.folder / "faults.json"
            state = json.loads(control.read_text()) if control.exists() else {}
            value = state.get(kind, {})
            if value.get("remaining", 0) > 0 and path.startswith(value.get("path", "")):
                value["remaining"] -= 1
                temporary = control.with_suffix(".pending")
                temporary.write_text(json.dumps(state))
                temporary.replace(control)
                return value
        return None

    def get_request(self):
        peer, address = self.socket.accept()
        if self.fault("tls"):
            peer.shutdown(socket.SHUT_RDWR)
            peer.close()
            raise OSError("injected TLS connection loss")
        peer.settimeout(20)
        try:
            return self.context.wrap_socket(peer, server_side=True), address
        except BaseException:
            peer.close()
            raise


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(json.dumps({"at": time.time(), "request": args[0] if args else format}), flush=True)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if fault := self.server.fault("delay", path):
            time.sleep(min(fault.get("seconds", 2), 30))
        if self.server.fault("http", path):
            self.send_error(503, "Injected temporary failure")
            return
        if path == f"/repos/{self.server.repository}/releases/latest":
            data = (self.server.folder / "latest.json").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        prefix = f"/{self.server.repository}/releases/download/"
        parts = path[len(prefix):].split("/") if path.startswith(prefix) else []
        if len(parts) != 2 or not parts[0].startswith("v") or any(p in {"", ".", ".."} for p in parts):
            self.send_error(404)
            return
        artifact = self.server.folder / parts[0] / parts[1]
        if not artifact.is_file() or artifact.is_symlink():
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(artifact.stat().st_size))
        self.end_headers()
        with artifact.open("rb") as source:
            while data := source.read(256 * 1024):
                self.wfile.write(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--repository", default="muggle-stack/cc-remote")
    Server(parser.parse_args()).serve_forever()
