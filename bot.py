"""Telegram control plane for BackPack Smart Tunnel Manager."""
import fcntl
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.request import Request, urlopen

from manager import State, benchmark, endpoint, mappings, name, port, remote
from updater import update

MENU = """🎒 BackPack Smart Tunnel Manager

ثبت یا ویرایش سرور:
/server NAME IP SSH_PORT USER

ایجاد پیش‌نویس:
/new TUNNEL IRAN ABROAD CONTROL_PORT

مدیریت پورت‌ها:
/ports TUNNEL 443,8443,2053:3000

تست و انتخاب روش:
/test TUNNEL
/test TUNNEL auto

تأیید نصب دستی:
/install TUNNEL

/serverlist — سرورها
/tunnels — تانل‌ها و پیش‌نویس‌ها
/status TUNNEL — وضعیت و بررسی پورت‌ها
/repair TUNNEL on — فعال‌سازی تعمیر خودکار
/repair TUNNEL off — غیرفعال‌سازی تعمیر خودکار
/rollback TUNNEL — بازگشت به نسخهٔ قبل
/recover — تکمیل بازگردانی متوقف‌شده
/license — مدیریت لایسنس محلی
/update — دریافت نسخهٔ جدید ربات از GitHub
"""


class Telegram:
    def __init__(self, token):
        self.url = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, **data):
        request = Request(self.url + method, data=json.dumps(data).encode(),
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=35) as response:
                return json.load(response)["result"]
        except Exception:
            # urllib exceptions may contain the URL and bot token.
            raise RuntimeError("ارتباط با API تلگرام برقرار نشد.") from None

    def send(self, chat, message):
        while message:
            self.call("sendMessage", chat_id=chat, text=message[:3500])
            message = message[3500:]


class Bot:
    def __init__(self, state, telegram, admins):
        self.state = state
        self.data = state.data
        self.tg = telegram
        self.admins = admins
        self.servers = self.data.setdefault("servers", {})
        self.repair_attempts = {}
        self.last_health = {}

    def execute(self, command, args, chat):
        if command in {"/start", "/help"}:
            return MENU
        if command == "/server" and len(args) == 4:
            label = name(args[0])
            host = endpoint(*args[1:])
            remote(host, "online", ports=[])
            self.servers[label] = host
            self.state.save()
            return f"سرور {label} ثبت شد. احراز هویت SSH با کلید انجام شد."
        if command == "/serverlist" and not args:
            return "\n".join(f"{label} · {host['user']} @ {host['ip']} · SSH {host['port']}"
                             for label, host in self.servers.items()) or "سروری ثبت نشده است."
        if command == "/new" and len(args) == 4:
            label, iran, abroad, control = args
            label = name(label)
            if label in self.data["live"]:
                raise ValueError("برای تانل فعال، پورت‌ها را با /ports ویرایش کنید.")
            if self.servers[iran]["ip"] == self.servers[abroad]["ip"]:
                raise ValueError("برای این تانل دو سرور متفاوت تعریف کنید.")
            self.data["drafts"][label] = {
                "iran": self.servers[iran], "abroad": self.servers[abroad],
                "control": port(control), "token": secrets.token_hex(32), "ports": [],
            }
            self.state.save()
            return "پیش‌نویس ثبت شد. پورت‌ها را با /ports وارد کنید."
        if command == "/tunnels" and not args:
            return "\n".join(
                f"{label} · {plan['iran']['ip']} → {plan['abroad']['ip']} · "
                + ("فعال" if label in self.data["live"] else "پیش‌نویس")
                for label, plan in self.data["drafts"].items()
            ) or "تانلی ثبت نشده است."
        if command == "/ports" and len(args) >= 2:
            label = name(args[0])
            plan = {**self.data["drafts"][label], "ports": mappings(" ".join(args[1:]))}
            plan.pop("transport", None)
            self.data["drafts"][label] = plan
            self.state.save()
            return "پورت‌ها ثبت شدند. " + ", ".join(f"{a} → {b}" for a, b in plan["ports"])
        if command == "/test" and (len(args) == 1 or len(args) == 2 and args[1] == "auto"):
            label = name(args[0])
            self.state.recover()
            plan = self.data["drafts"][label]
            if not plan["ports"]:
                raise ValueError("ابتدا با /ports پورت‌های کانفیگ را وارد کنید.")
            self.tg.send(chat, "آزمون محدود مسیر شروع شد.")
            best, reports = benchmark(plan, self.data["live"].get(label))
            self.data["drafts"][label] = {**plan, "transport": best}
            self.state.save()
            lines = [f"روش پیشنهادی {best}", f"پورت کنترل {plan['control']}"]
            lines.append("مسیر " + plan["iran"]["ip"] + " → " + plan["abroad"]["ip"])
            lines.append("پورت‌ها " + ", ".join(f"{a} → {b}" for a, b in plan["ports"]))
            for method, report in reports.items():
                seconds = report["seconds"]
                metric = f" · {seconds * 1000:.1f} ms · {0.524288 / seconds:.2f} Mbps رفت" if seconds else ""
                lines.append(f"{method} · {report['success']}/3 نمونهٔ موفق{metric}")
            if len(args) == 2:
                self.state.deploy(label)
                lines.append("نصب و بررسی سلامت انجام شد.")
            else:
                lines.append(f"برای تأیید نصب /install {label}")
            return "\n".join(lines)
        if command == "/install" and len(args) == 1:
            self.state.deploy(name(args[0]))
            return "نصب و بررسی سلامت انجام شد."
        if command == "/status" and len(args) == 1:
            label = name(args[0])
            plan = self.data["live"][label]
            status = "سالم" if self.state.healthy(label, plan) else "نیازمند بررسی"
            return f"{label} · {plan['transport']} · {status}"
        if command == "/repair" and len(args) == 2 and args[1] in {"on", "off"}:
            label = name(args[0])
            if label not in self.data["live"]:
                raise KeyError(label)
            enabled = set(self.data["repair"])
            if args[1] == "on":
                enabled.add(label)
            else:
                enabled.discard(label)
            self.data["repair"] = sorted(enabled)
            self.state.save()
            return f"تعمیر خودکار {label} · {args[1]}"
        if command == "/rollback" and len(args) == 1:
            self.state.rollback(name(args[0]))
            return "تنظیمات قبلی بازگردانده شد."
        if command == "/recover" and not args:
            self.state.recover()
            return "بازگردانی معوقی وجود ندارد."
        if command == "/license":
            return self.license(args)
        if command == "/update" and not args:
            self.state.recover()
            self.tg.send(chat, "در حال بررسی شاخهٔ main در GitHub.")
            revision = update()
            if revision:
                self.data["update_notice"] = [chat, revision]
                self.state.save()
                return None
            return "ربات به‌روز است."
        return "دستور یا آرگومان‌ها معتبر نیستند. /help"

    def license(self, args):
        records = self.data["licenses"]
        if args == ["list"]:
            return "\n".join(
                f"{key} · {time.strftime('%Y-%m-%d', time.gmtime(expiry))} UTC"
                for key, expiry in records.items()
            ) or "لایسنسی صادر نشده است."
        if len(args) == 2 and args[0] == "issue":
            days = int(args[1])
            if not 1 <= days <= 3650:
                raise ValueError("مدت لایسنس باید بین ۱ و ۳۶۵۰ روز باشد.")
            key = secrets.token_urlsafe(18)
            records[key] = int(time.time()) + days * 86400
            self.state.save()
            return f"لایسنس {days} روزه\n{key}"
        if len(args) == 2 and args[0] == "revoke":
            del records[args[1]]
            self.state.save()
            return "لایسنس لغو شد."
        if len(args) == 3 and args[0] == "assign":
            label = name(args[1])
            plan = {**self.data["drafts"][label], "license": args[2]}
            self.state.check_license(plan)
            self.data["drafts"][label] = plan
            self.state.save()
            return "لایسنس به پیش‌نویس اختصاص یافت."
        return "/license issue DAYS\n/license list\n/license assign TUNNEL CODE\n/license revoke CODE"

    def monitor(self):
        if self.data["pending"]:
            return
        for label, plan in self.data["live"].items():
            try:
                healthy = self.state.healthy(label, plan)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                healthy = False
            previous = self.last_health.get(label)
            now = time.monotonic()
            repair_due = now - self.repair_attempts.get(label, -600) >= 300
            if not healthy and label in self.data["repair"] and repair_due:
                self.repair_attempts[label] = now
                try:
                    self.state.check_license(plan)
                    for side in ("iran", "abroad"):
                        remote(plan[side], "restart", name=label)
                    time.sleep(3)
                    healthy = self.state.healthy(label, plan)
                except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
                    healthy = False
                message = (f"تانل {label} پس از راه‌اندازی مجدد سالم است." if healthy else
                           f"تانل {label} پس از تلاش برای تعمیر همچنان نیازمند بررسی است.")
            elif healthy == previous or healthy and previous is None:
                continue
            else:
                message = f"تانل {label} دوباره سالم است." if healthy else f"تانل {label} نیازمند بررسی است."
            self.last_health[label] = healthy
            for admin in self.admins:
                try:
                    self.tg.send(admin, message)
                except RuntimeError:
                    logging.warning("Could not send health notification")

    def run(self):
        notice = self.data.get("update_notice")
        if notice:
            try:
                self.tg.send(notice[0], f"ربات با نسخهٔ {notice[1]} دوباره راه‌اندازی شد.")
            except RuntimeError:
                logging.warning("Could not send update notification")
            else:
                del self.data["update_notice"]
                self.state.save()
        last_monitor = time.monotonic()
        while True:
            try:
                events = self.tg.call("getUpdates", offset=self.data.get("offset", 0),
                                      timeout=15, allowed_updates=["message"])
            except RuntimeError:
                logging.warning("Telegram polling failed")
                time.sleep(5)
                continue
            for event in events:
                self.data["offset"] = event["update_id"] + 1
                self.state.save()
                msg = event.get("message", {})
                chat = msg.get("chat", {})
                sender = msg.get("from", {})
                tokens = msg.get("text", "").split()
                if chat.get("type") != "private" or sender.get("id") not in self.admins or not tokens:
                    continue
                command, *args = tokens
                try:
                    reply = self.execute(command.split("@")[0], args, chat["id"])
                except KeyError:
                    reply = "سرور، تانل، نسخهٔ قبلی یا لایسنس پیدا نشد."
                except (RuntimeError, ValueError) as error:
                    reply = str(error)
                except (OSError, subprocess.SubprocessError):
                    reply = "عملیات ناموفق بود. وضعیت شبکه و سرویس را بررسی کنید."
                if reply is None:
                    return 75
                try:
                    self.tg.send(chat["id"], reply)
                except RuntimeError:
                    logging.warning("Could not send command response")
            if time.monotonic() - last_monitor >= 60:
                self.monitor()
                last_monitor = time.monotonic()


def main():
    logging.basicConfig(level=logging.INFO)
    path = Path(os.environ.get("BOT_STATE", "/var/lib/atuotunnel/state.json"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    admins = {int(value.strip()) for value in os.environ["BOT_ADMIN_IDS"].split(",")}
    telegram = Telegram(os.environ["BOT_TOKEN"])
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Bot is already running.")
        raise SystemExit(Bot(State(path), telegram, admins).run())


if __name__ == "__main__":
    main()
