"""BackPack deployment lifecycle and bounded path comparison."""
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import statistics
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent
TRANSPORTS = ("tcp", "tcpmux")
MAX_PORTS = 16


def name(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", value):
        raise ValueError("نام باید با حرف انگلیسی شروع شود و حداکثر ۲۴ کاراکتر باشد.")
    return value


def port(value):
    if not re.fullmatch(r"[0-9]{1,5}", str(value)) or not 1 <= int(value) <= 65535:
        raise ValueError("پورت باید عددی بین ۱ و ۶۵۵۳۵ باشد.")
    return int(value)


def endpoint(value, ssh_port, user):
    address = ip_address(value)
    if address.version == 6 and address.scope_id:
        raise ValueError("آدرس IP بدون شناسهٔ رابط وارد کنید.")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("نام کاربری SSH معتبر نیست.")
    return {"ip": str(address), "port": port(ssh_port), "user": user}


def mappings(value):
    pairs = {}
    for raw in value.replace("،", ",").split(","):
        parts = raw.strip().split(":")
        source = port(parts[0].strip())
        if len(parts) > 2 or source in pairs:
            raise ValueError("هر پورت ورودی باید دقیقاً یک‌بار وارد شود.")
        pairs[source] = port(parts[-1].strip())
    if not 1 <= len(pairs) <= MAX_PORTS:
        raise ValueError(f"بین ۱ تا {MAX_PORTS} پورت تعریف کنید.")
    return [[source, target] for source, target in pairs.items()]


def remote(host, action, **data):
    source = (ROOT / "remote.py").read_text()
    privilege = "" if host["user"] == "root" else "sudo -n "
    command = privilege + "python3 -c " + shlex.quote(source)
    args = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
        "-o", "ClearAllForwardings=yes", "-p", str(host["port"]),
        "-l", host["user"], "--", host["ip"], command,
    ]
    process = subprocess.run(args, input=json.dumps({"action": action, **data}),
                             text=True, capture_output=True, timeout=45)
    if process.returncode:
        # Keep raw SSH and remote command output out of Telegram.
        raise RuntimeError(f"عملیات {action} روی {host['ip']} ناموفق بود.")
    return json.loads(process.stdout)


def configs(plan, name_, transport, control=None, forwards=None):
    control = plan["control"] if control is None else control
    listen = "" if forwards is None else "127.0.0.1:"
    forwards = plan["ports"] if forwards is None else forwards
    address = plan["iran"]["ip"]
    if ":" in address:
        address = f"[{address}]"
    shared = f'transport = "{transport}"\ntoken = "{plan["token"]}"\n'
    ports = [f"{listen}{source}=127.0.0.1:{target}" for source, target in forwards]
    return {
        "iran": {"name": name_, "config": (
            "[server]\n" + shared + f'bind_addr = ":{control}"\nports = {json.dumps(ports)}\n'
        )},
        "abroad": {"name": name_, "config": (
            "[client]\n" + shared + f'remote_addr = "{address}:{control}"\n'
        )},
    }


def check_ports(plan, current=None):
    ingress = {source for source, _ in plan["ports"]}
    if plan["control"] in ingress:
        raise ValueError("پورت کنترل باید با پورت‌های ورودی متفاوت باشد.")
    requested = ingress | {plan["control"]}
    if plan["iran"]["port"] in requested:
        raise ValueError("پورت کنترل یا ورودی با SSH ایران تداخل دارد.")
    targets = [target for _, target in plan["ports"]]
    if plan["abroad"]["port"] in targets:
        raise ValueError("پورت مقصد با SSH خارج تداخل دارد.")
    if current:
        requested -= {source for source, _ in current["ports"]} | {current["control"]}
    if requested:
        remote(plan["iran"], "free", ports=sorted(requested))
    if not all(remote(plan["abroad"], "online", ports=sorted(set(targets)))):
        raise RuntimeError("یک یا چند سرویس مقصد روی loopback سرور خارج در دسترس نیست.")


def benchmark(plan, current=None):
    check_ports(plan, current)
    for side in ("iran", "abroad"):
        remote(plan[side], "install")
    reports = {}
    for transport in TRANSPORTS:
        suffix = "test-" + secrets.token_hex(6)
        # Use the production control port on first install. An existing tunnel
        # owns that port during edits, so an isolated port is used in that case.
        control, ingress = remote(plan["iran"], "free", ports=[
            plan["control"] if not current or current["control"] != plan["control"] else 0, 0,
        ])
        if control == ingress:
            ingress = remote(plan["iran"], "free", ports=[0])[0]
        target = remote(plan["abroad"], "free", ports=[0])[0]
        test_plan = {**plan, "token": secrets.token_hex(32)}
        settings = configs(test_plan, suffix, transport, control, [(ingress, target)])
        samples = []
        try:
            remote(plan["abroad"], "echo", name=suffix + "-echo", port=target)
            for side, args in settings.items():
                remote(plan[side], "test", **args)
            time.sleep(2)
            for _ in range(3):
                try:
                    samples.append(remote(plan["iran"], "sample", port=ingress))
                except RuntimeError:
                    continue
        except RuntimeError:
            pass
        finally:
            for side, unit in (("abroad", suffix), ("iran", suffix), ("abroad", suffix + "-echo")):
                try:
                    remote(plan[side], "stop-test", name=unit)
                except (RuntimeError, subprocess.TimeoutExpired):
                    # Transient units have an independent systemd time limit.
                    pass
        reports[transport] = {"success": len(samples),
                              "seconds": statistics.median(samples) if samples else None}
    ranked = sorted(TRANSPORTS, key=lambda item: (
        -reports[item]["success"], reports[item]["seconds"] or float("inf")
    ))
    if not reports[ranked[0]]["success"]:
        raise RuntimeError("هیچ روش تونل در آزمون پاسخ نداد. دسترسی پورت کنترل را بررسی کنید.")
    return ranked[0], reports


class State:
    def __init__(self, path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {
            "drafts": {}, "live": {}, "previous": {}, "pending": None, "repair": [], "licenses": {},
        }

    def save(self):
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as file:
            try:
                json.dump(self.data, file)
                file.flush()
                os.fsync(file.fileno())
                os.replace(file.name, self.path)
            finally:
                Path(file.name).unlink(missing_ok=True)

    def healthy(self, tunnel, plan):
        return (
            all(remote(plan[side], "connected", name=tunnel) for side in ("iran", "abroad"))
            and all(remote(plan["iran"], "online", ports=[source for source, _ in plan["ports"]]))
            and all(remote(plan["abroad"], "online", ports=[target for _, target in plan["ports"]]))
        )

    def snapshot(self, tunnel, plan):
        return {side: remote(plan[side], "snapshot", name=tunnel) for side in ("iran", "abroad")}

    def restore(self, transaction):
        failures = []
        for side in ("iran", "abroad"):
            try:
                remote(transaction["hosts"][side], "restore", name=transaction["name"],
                       snapshot=transaction["files"][side])
            except (RuntimeError, subprocess.TimeoutExpired):
                failures.append(side)
        if failures:
            raise RuntimeError("بازگردانی کامل نشد. پس از دسترسی SSH، /recover را اجرا کنید.")

    def recover(self):
        if self.data["pending"]:
            self.restore(self.data["pending"])
            self.data["pending"] = None
            self.save()

    def deploy(self, tunnel):
        self.recover()
        plan = self.data["drafts"][tunnel]
        if "transport" not in plan:
            raise ValueError("ابتدا /test را اجرا کنید.")
        self.check_license(plan)
        current = self.data["live"].get(tunnel)
        check_ports(plan, current)
        transaction = {"name": tunnel, "hosts": plan, "files": self.snapshot(tunnel, plan),
                       "plan": current}
        if not current and any(snap["unit"] or snap["config"] or snap["active"]
                               for snap in transaction["files"].values()):
            raise RuntimeError("این نام روی سرور قبلاً استفاده شده است. نام دیگری انتخاب کنید.")
        self.data["pending"] = transaction
        self.save()
        try:
            for side, args in configs(plan, tunnel, plan["transport"]).items():
                remote(plan[side], "apply", **args)
            for _ in range(4):
                time.sleep(2)
                if self.healthy(tunnel, plan):
                    break
            else:
                raise RuntimeError("بررسی سرویس‌ها یا پورت‌ها پس از نصب ناموفق بود.")
        except Exception:
            self.recover()
            raise
        self.data["live"][tunnel] = plan
        self.data["previous"][tunnel] = transaction
        self.data["pending"] = None
        self.save()

    def rollback(self, tunnel):
        self.recover()
        previous = self.data["previous"][tunnel]
        current = self.data["live"][tunnel]
        transaction = {"name": tunnel, "hosts": current, "files": self.snapshot(tunnel, current),
                       "plan": current}
        self.data["pending"] = transaction
        self.save()
        try:
            self.restore(previous)
            if previous["plan"] and not self.healthy(tunnel, previous["plan"]):
                raise RuntimeError("بررسی نسخهٔ قبلی ناموفق بود.")
        except Exception:
            self.recover()
            raise
        if previous["plan"]:
            self.data["live"][tunnel] = previous["plan"]
            self.data["previous"][tunnel] = transaction
        else:
            del self.data["live"][tunnel]
            del self.data["previous"][tunnel]
            if tunnel in self.data["repair"]:
                self.data["repair"].remove(tunnel)
        self.data["pending"] = None
        self.save()

    def check_license(self, plan):
        code = plan.get("license")
        if code and self.data["licenses"].get(code, 0) <= time.time():
            raise ValueError("لایسنس منقضی یا لغو شده است.")
