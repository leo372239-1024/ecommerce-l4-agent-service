import json
import socket

with socket.socket(socket.AF_UNIX) as client:
    client.settimeout(3)
    client.connect("/run/sandbox/broker.sock")
    client.sendall(
        json.dumps({"tool": "money_total", "arguments": {"amounts": ["0.10", "0.20"]}}).encode()
        + b"\n"
    )
    assert json.loads(client.recv(8192))["result"]["total"] == "0.30"
