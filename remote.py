"""SSH entry point. The controller supplies this source and a JSON request."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen

DIRECTORY = Path("/etc/atuotunnel")
BINARY = Path("/usr/local/lib/atuotunnel/backpack-v2.0.0")
DIGESTS = {
    "x86_64": ("amd64", "6e38749a1d749cbf11eb293457ddc94142a96659350acf6047fc7ef44a6115d3"),
    "aarch64": ("arm64", "7f709943079b257856349ddbb370cdd1b386ea7059e11584d9ae1fcab40616fe"),
}
ECHO = """
import socketserver,sys
class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        data = self.request.makefile('rb').read(65536)
        if len(data) == 65536:
            self.request.sendall(data)
with socketserver.TCPServer(('127.0.0.1', int(sys.argv[1])), Handler) as server:
    server.serve_forever()
"""


def run(*args, check=True):
    return subprocess.run(args, check=check, timeout=20, capture_output=True)


def atomic(path, data, mode=0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        try:
            os.fchmod(temp.fileno(), mode)
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(temp.name, path)
        finally:
            Path(temp.name).unlink(missing_ok=True)


def install():
    if BINARY.is_file():
        return
    arch, digest = DIGESTS[platform.machine()]
    url = f"https://github.com/AminMGMT/BackPack/releases/download/v2.0.0/backpack_linux_{arch}.tar.gz"
    with urlopen(url, timeout=8) as response:
        archive = response.read(32 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != digest:
        raise ValueError("BackPack checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive)) as package:
        with package.extractfile("backpack") as executable:
            atomic(BINARY, executable.read(), 0o755)


def online(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def free(port):
    if socket.has_ipv6:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind(("::", port))
            return sock.getsockname()[1]
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", port))
        return sock.getsockname()[1]


def result(req):
    action = req["action"]
    if action == "install":
        return install()
    if action == "free":
        return [free(port) for port in req["ports"]]
    if action == "online":
        return [online(port) for port in req["ports"]]
    if action == "sample":
        payload = os.urandom(65536)
        start = time.monotonic()
        with socket.create_connection(("127.0.0.1", req["port"]), timeout=2) as sock:
            sock.sendall(payload)
            if sock.makefile("rb").read(len(payload)) != payload:
                raise ValueError("incomplete echo")
        return round(time.monotonic() - start, 5)

    name = req["name"]
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
        raise ValueError("invalid service name")
    unit = f"atuotunnel-{name}.service"
    if action in {"test", "echo", "stop-test"}:
        config = Path("/run/atuotunnel") / f"atuotunnel-{name}.toml"
    else:
        config = DIRECTORY / f"atuotunnel-{name}.toml"
    unit_path = Path("/etc/systemd/system") / unit
    if action == "status":
        return run("systemctl", "is-active", "--quiet", unit, check=False).returncode == 0
    if action == "connected":
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(2)
                connection.connect(f"/run/backpack/atuotunnel-{name}.sock")
                connection.sendall(b'{"op":"status"}\n')
                response = json.loads(connection.makefile("rb").readline(65536))
                return response["ok"] and response["body"]["connected"]
        except OSError:
            return False
    if action == "restart":
        return run("systemctl", "restart", unit).returncode == 0
    if action == "stop-test":
        run("systemctl", "stop", unit, check=False)
        config.unlink(missing_ok=True)
        return
    if action in {"test", "echo"}:
        if action == "echo":
            command = [sys.executable, "-c", ECHO, str(req["port"])]
        else:
            atomic(config, req["config"].encode())
            command = [str(BINARY), "-c", str(config)]
        run("systemd-run", "--quiet", "--collect", f"--unit={unit}",
            "--property=RuntimeMaxSec=75", "--property=UMask=0077", *command)
        return

    if action == "snapshot":
        return {
            "config": base64.b64encode(config.read_bytes()).decode() if config.exists() else None,
            "unit": base64.b64encode(unit_path.read_bytes()).decode() if unit_path.exists() else None,
            "enabled": run("systemctl", "is-enabled", "--quiet", unit, check=False).returncode == 0,
            "active": result({"action": "status", "name": name}),
        }
    if action == "apply":
        with tempfile.NamedTemporaryFile(suffix=".toml") as candidate:
            candidate.write(req["config"].encode())
            candidate.flush()
            run(str(BINARY), "check", "-c", candidate.name)
        atomic(config, req["config"].encode())
        service = (
            "[Unit]\nDescription=BackPack Smart Tunnel\nAfter=network-online.target\n"
            "Wants=network-online.target\n[Service]\n"
            f"ExecStart={BINARY} -c {config}\nRestart=on-failure\nRestartSec=4\n"
            "TimeoutStopSec=8\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n"
        )
        atomic(unit_path, service.encode(), 0o644)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", unit)
        run("systemctl", "restart", unit)
        return
    if action == "restore":
        snapshot = req["snapshot"]
        if result({"action": "status", "name": name}):
            run("systemctl", "stop", unit)
        if unit_path.exists():
            run("systemctl", "disable", unit)
        for path, key, mode in ((config, "config", 0o600), (unit_path, "unit", 0o644)):
            if snapshot[key] is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, base64.b64decode(snapshot[key]), mode)
        run("systemctl", "daemon-reload")
        if snapshot["enabled"]:
            run("systemctl", "enable", unit)
        if snapshot["active"]:
            run("systemctl", "start", unit)
        return
    raise ValueError("unknown operation")


if __name__ == "__main__":
    try:
        print(json.dumps(result(json.load(sys.stdin))))
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
