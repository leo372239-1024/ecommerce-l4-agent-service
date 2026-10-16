"""No TCP listener. A bounded Unix socket broker spawns only the immutable runner."""

import json
import os
import socketserver
import subprocess
import sys
import threading

PATH = "/run/sandbox/broker.sock"
SLOTS = threading.BoundedSemaphore(2)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        if not SLOTS.acquire(blocking=False):
            self.wfile.write(b'{"ok":false,"error":"sandbox_busy"}\n')
            return
        try:
            raw = self.rfile.readline(65537)
            if len(raw) > 65536 or not raw.endswith(b"\n"):
                raise ValueError("request_limit")
            output = subprocess.run(
                [sys.executable, "-I", "-B", "/sandbox/runner.py"],
                input=raw,
                capture_output=True,
                timeout=2,
                check=True,
                env={"PATH": "/usr/local/bin:/usr/bin"},
            ).stdout
            if len(output) > 8192:
                raise ValueError("output_limit")
            json.loads(output)
            self.wfile.write(output + b"\n")
        except (ValueError, OSError, subprocess.SubprocessError):
            self.wfile.write(b'{"ok":false,"error":"sandbox_execution_failed"}\n')
        finally:
            SLOTS.release()


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 4


if __name__ == "__main__":
    if os.path.exists(PATH):
        os.unlink(PATH)
    with Server(PATH, Handler) as server:
        os.chmod(PATH, 0o600)
        server.serve_forever()
