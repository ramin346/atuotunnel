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

from manager import (State, benchmark, credential_path, endpoint, mappings, name, port,
                     remote, save_password)
from updater import update

ADMIN_HELP = """🎒 BackPack Smart Tunnel Manager

ثبت یا ویرایش سرور:
/server — ثبت مرحله‌ای سرور

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
/grant USER_ID SERVER... — تعیین سرورهای مجاز کاربر
/update — دریافت نسخهٔ جدید ربات از GitHub
"""


USER_HELP = """🎒 BackPack Smart Tunnel Manager
لایسنس را فعال کنید و برای ساخت تانل از سرورهای اختصاص‌یافته استفاده کنید.
شناسهٔ تلگرام شما: {user}
"""

BUTTONS = {
    "new": ("ایجاد تانل", "/new", "نام تانل، نام سرور ایران، نام سرور خارج و پورت کنترل را وارد کنید.\nنمونه: xray ir de 3080"),
    "ports": ("مدیریت پورت‌ها", "/ports", "نام تانل و نگاشت پورت‌ها را وارد کنید.\nنمونه: xray 443,8443,2053:3000"),
    "test": ("تست هوشمند", "/test", "نام تانل را وارد کنید. برای نصب خودکار پس از تست، auto را اضافه کنید."),
    "install": ("تأیید نصب", "/install", "نام تانل تست‌شده را وارد کنید."),
    "serverlist": ("سرورهای من", "/serverlist", None),
    "tunnels": ("تانل‌های من", "/tunnels", None),
    "status": ("وضعیت تانل", "/status", "نام تانل را وارد کنید."),
    "repair": ("تعمیر خودکار", "/repair", "نام تانل و on یا off را وارد کنید.\nنمونه: xray on"),
    "rollback": ("بازگشت تنظیمات", "/rollback", "نام تانل را برای بازگردانی وارد کنید."),
    "activate": ("فعال‌سازی لایسنس", "/activate", "کد لایسنس را وارد کنید."),
    "license": ("مدیریت لایسنس", "/license", None),
    "license-issue": ("صدور لایسنس", "/license issue", "تعداد روزهای اعتبار را وارد کنید."),
    "license-list": ("فهرست لایسنس‌ها", "/license list", None),
    "license-revoke": ("لغو لایسنس", "/license revoke", "کد لایسنس برای لغو را وارد کنید."),
    "server": ("ثبت سرور", "/server", None),
    "grant": ("دسترسی سرورها", "/grant", "شناسهٔ عددی کاربر و نام سرورهای مجاز را وارد کنید."),
    "update": ("به‌روزرسانی ربات", "/update", None),
    "confirm-update": ("تأیید به‌روزرسانی", "/update", None),
}
LICENSE_BUTTONS = ("license-issue", "license-list", "license-revoke")
ADMIN_ROWS = (("server", "grant"), ("license", "update"))
COMMON_ROWS = (("new", "ports"), ("test", "install"), ("serverlist", "tunnels"),
               ("status", "repair"), ("rollback",))


def keyboard(admin):
    rows = COMMON_ROWS + ADMIN_ROWS if admin else (("activate", "server"),) + COMMON_ROWS
    return {"inline_keyboard": [
        [{"text": BUTTONS[key][0], "callback_data": key} for key in row] for row in rows
    ]}


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

    def send(self, chat, message, markup=None):
        while message:
            chunk, message = message[:3500], message[3500:]
            options = {"reply_markup": markup} if markup and not message else {}
            self.call("sendMessage", chat_id=chat, text=chunk, **options)


class Bot:
    def __init__(self, state, telegram, admins, user=None):
        self.state = state
        self.data = state.data
        self.tg = telegram
        self.admins = admins
        self.user = user
        self.directory = state.root.data
        self.servers = self.directory.setdefault("servers", {})
        self.prompts = {}
        self.server_form = {}
        self.sessions = {}
        self.repair_attempts = {}
        self.last_health = {}

    def available_servers(self):
        if self.user is None:
            return self.servers
        allowed = self.directory.get("grants", {}).get(str(self.user), [])
        shared = {label: self.servers[label] for label in allowed if label in self.servers}
        return {**shared, **self.data.get("servers", {})}

    def valid_license(self):
        code = self.data.get("active_license")
        return (self.directory.get("license_owners", {}).get(code) == self.user
                and self.directory["licenses"].get(code, 0) > time.time())

    def require_access(self, plan=None):
        if self.user is None:
            return
        if not self.valid_license():
            raise ValueError("برای این عملیات ابتدا لایسنس معتبر فعال کنید.")
        if plan and not self.has_servers(plan):
            raise ValueError("دسترسی به سرورهای این تانل را از مدیر دریافت کنید.")

    def has_servers(self, plan):
        if self.user is None:
            return True
        available = self.available_servers().values()
        return all(plan[side] in available for side in ("iran", "abroad"))

    def activate(self, code):
        if self.directory["licenses"].get(code, 0) <= time.time():
            raise ValueError("لایسنس نامعتبر، منقضی یا لغوشده است.")
        owners = self.directory.setdefault("license_owners", {})
        if owners.get(code, self.user) != self.user:
            raise ValueError("این لایسنس قبلاً برای حساب دیگری فعال شده است.")
        owners[code] = self.user
        self.data["active_license"] = code
        self.state.save()
        return "لایسنس فعال شد. سرورها را در «سرورهای من» ببینید یا با دکمهٔ «ثبت سرور» سرور خودتان را اضافه کنید."

    def start_server_form(self, label=None):
        self.require_access()
        self.server_form = {"step": "ip", "label": name(label) if label is not None else None}
        return "آدرس IP سرور را بفرستید. برای پورت غیراستاندارد، IP:PORT یا [IPv6]:PORT را وارد کنید. /cancel برای لغو"

    def register_server(self, label, host, password):
        self.require_access()
        if self.user is not None:
            host["credential_owner"] = self.user
        inventory = self.servers if self.user is None else self.data.setdefault("servers", {})
        if label is None:
            label = next((key for key, saved in inventory.items() if saved == host), None)
            if label is None:
                used = self.available_servers()
                index = 1
                while f"server-{index}" in used:
                    index += 1
                label = f"server-{index}"
        if label not in inventory and label in self.available_servers():
            raise ValueError("این نام برای یک سرور اختصاص‌یافته استفاده شده است.")
        remote(host, "online", password=password, ports=[])
        if password:
            save_password(host, password)
        else:
            credential_path(host).unlink(missing_ok=True)
        inventory[label] = host
        self.state.save()
        return f"✅ اتصال SSH تأیید شد و سرور با نام {label} ثبت شد."

    def continue_server_form(self, text, chat, message):
        form = self.server_form
        if form["step"] != "password":
            self.require_access()
        if form["step"] == "ip":
            value = text.strip()
            address, ssh_port = value, "22"
            if value.startswith("[") and "]:" in value:
                address, ssh_port = value[1:].split("]:", 1)
            elif value.count(":") == 1:
                address, ssh_port = value.split(":", 1)
            host = endpoint(address, ssh_port, "root")
            form.update(step="user", ip=host["ip"], port=host["port"])
            return "نام کاربری SSH را بفرستید."
        if form["step"] == "user":
            form["host"] = endpoint(form["ip"], form["port"], text.strip())
            form["step"] = "password"
            return ("رمز SSH را بفرستید." if self.user is not None else
                    "رمز SSH را بفرستید. اگر از کلید ازپیش‌تنظیم‌شده استفاده می‌کنید، /key بزنید.")
        use_key = self.user is None and text == "/key"
        if not use_key:
            try:
                self.tg.call("deleteMessage", chat_id=chat, message_id=message)
            except RuntimeError:
                logging.warning("Could not delete SSH password message")
            if not text or any(ch in text for ch in "\r\n\0") or len(text.encode()) > 1024:
                raise ValueError("رمز معتبر را در یک خط بفرستید.")
        self.require_access()
        self.tg.send(chat, "در حال بررسی اتصال SSH…")
        try:
            reply = self.register_server(form["label"], form["host"], "" if use_key else text)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return ("❌ ثبت سرور انجام نشد. IP، پورت، نام کاربری، روش احراز هویت و کلید میزبان را بررسی کنید. "
                    "برای آزمون دوباره رمز را بفرستید یا با /cancel فرم را ببندید.")
        self.server_form = {}
        return reply

    def execute(self, command, args, chat):
        if command in {"/start", "/help"}:
            if self.user is None:
                return ADMIN_HELP
            license_status = "فعال" if self.valid_license() else "نیازمند فعال‌سازی"
            return USER_HELP.format(user=self.user) + f"وضعیت لایسنس: {license_status}"
        if self.user is not None:
            if command == "/activate" and len(args) == 1:
                return self.activate(args[0])
            if command in {"/grant", "/update", "/license", "/recover"}:
                raise ValueError("این دستور در اختیار مدیر ربات است.")
            if command in {"/new", "/ports", "/test", "/install", "/repair", "/rollback"}:
                self.require_access()
                if command != "/new" and args:
                    key = name(args[0])
                    for plan in (self.data["drafts"].get(key), self.data["live"].get(key),
                                 self.data["previous"].get(key, {}).get("plan")):
                        if plan:
                            self.require_access(plan)
        if command == "/grant" and args:
            user = str(int(args[0]))
            if int(user) <= 0:
                raise ValueError("شناسهٔ تلگرام باید مثبت باشد.")
            labels = list(dict.fromkeys(args[1:]))
            if any(label not in self.servers for label in labels):
                raise ValueError("ابتدا سرورها را با /server ثبت کنید.")
            self.directory.setdefault("grants", {})[user] = labels
            self.state.save()
            return f"سرورهای مجاز {user} · " + (", ".join(labels) or "بدون دسترسی")
        if command == "/server" and len(args) <= 1:
            return self.start_server_form(args[0] if args else None)
        if command == "/server" and len(args) == 4 and self.user is None:
            label = name(args[0])
            return self.register_server(label, endpoint(*args[1:]), "")
        if command == "/cancel" and not args:
            self.server_form = {}
            return "فرم لغو شد."
        if command == "/serverlist" and not args:
            return "\n".join(f"{label} · {host['user']} @ {host['ip']} · SSH {host['port']}"
                             for label, host in self.available_servers().items()) or "سروری ثبت نشده است."
        if command == "/new" and len(args) == 4:
            label, iran, abroad, control = args
            label = name(label)
            if label in self.data["live"]:
                raise ValueError("برای تانل فعال، پورت‌ها را با /ports ویرایش کنید.")
            servers = self.available_servers()
            if iran not in servers or abroad not in servers:
                raise ValueError("نام دو سرور مجاز را از بخش «سرورهای من» انتخاب کنید.")
            if servers[iran]["ip"] == servers[abroad]["ip"]:
                raise ValueError("برای این تانل دو سرور متفاوت تعریف کنید.")
            self.data["drafts"][label] = {
                "iran": servers[iran], "abroad": servers[abroad],
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
                self.require_access(plan)
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
            if not self.has_servers(plan):
                raise ValueError("دسترسی به سرورهای این تانل را از مدیر دریافت کنید.")
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
        if command == "/recover" and len(args) <= 1:
            state = self.state.scoped(int(args[0])) if args else self.state
            state.recover()
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
            if not self.has_servers(plan):
                continue
            try:
                healthy = self.state.healthy(label, plan)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                healthy = False
            previous = self.last_health.get(label)
            now = time.monotonic()
            repair_due = now - self.repair_attempts.get(label, -600) >= 300
            repair_allowed = self.user is None or self.valid_license()
            if not healthy and label in self.data["repair"] and repair_due and repair_allowed:
                self.repair_attempts[label] = now
                try:
                    self.require_access(plan)
                    self.state.check_license(plan)
                    for side in ("iran", "abroad"):
                        remote(plan[side], "restart", name=self.state.unit(label))
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
            for recipient in ({self.user} if self.user is not None else self.admins):
                try:
                    self.tg.send(recipient, message)
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
                                      timeout=15, allowed_updates=["message", "callback_query"])
            except RuntimeError:
                logging.warning("Telegram polling failed")
                time.sleep(5)
                continue
            for event in events:
                self.data["offset"] = event["update_id"] + 1
                self.state.save()
                if self.handle(event):
                    return 75
            if time.monotonic() - last_monitor >= 60:
                self.monitor()
                for user, data in self.directory.get("users", {}).items():
                    if data["live"]:
                        self.session(int(user)).monitor()
                last_monitor = time.monotonic()

    def session(self, user):
        if user in self.admins:
            return self
        if user not in self.sessions:
            self.sessions[user] = Bot(self.state.scoped(user), self.tg, self.admins, user=user)
        return self.sessions[user]

    def handle(self, event):
        callback = event.get("callback_query")
        msg = callback.get("message", {}) if callback else event.get("message", {})
        chat = msg.get("chat", {})
        user = (callback or msg).get("from", {}).get("id")
        if chat.get("type") != "private" or user != chat.get("id") or not isinstance(user, int):
            return False
        admin = user in self.admins
        session = self.session(user)
        markup = keyboard(admin)
        try:
            if callback:
                self.tg.call("answerCallbackQuery", callback_query_id=callback["id"])
                self.prompts.pop(user, None)
                key = callback.get("data")
                allowed = {item["callback_data"] for row in markup["inline_keyboard"] for item in row}
                if admin:
                    allowed.update((*LICENSE_BUTTONS, "confirm-update"))
                if key not in allowed:
                    return False
                session.server_form = {}
                if key == "license":
                    submenu = {"inline_keyboard": [
                        [{"text": BUTTONS[item][0], "callback_data": item}] for item in LICENSE_BUTTONS
                    ]}
                    self.tg.send(user, "مدیریت لایسنس", submenu)
                    return False
                if key == "update":
                    confirm = {"inline_keyboard": [[
                        {"text": BUTTONS["confirm-update"][0], "callback_data": "confirm-update"}
                    ]]}
                    self.tg.send(user, "نسخهٔ جدید ربات از شاخهٔ main دریافت شود؟", confirm)
                    return False
                _, command, prompt = BUTTONS[key]
                if prompt:
                    if key not in {"activate", "server", "grant", "status"}:
                        session.require_access()
                    self.prompts[user] = command
                    self.tg.send(user, prompt)
                    return False
                command, *args = command.split()
            else:
                message_text = msg.get("text")
                if not message_text:
                    return False
                if session.server_form and message_text not in {"/cancel", "/start", "/help"}:
                    reply = session.continue_server_form(message_text, user, msg["message_id"])
                    self.tg.send(user, reply, markup if not session.server_form else None)
                    return False
                tokens = message_text.split()
                if not tokens:
                    return False
                if tokens[0].startswith("/"):
                    if tokens[0] in {"/start", "/help"}:
                        session.server_form = {}
                    self.prompts.pop(user, None)
                    command, *args = tokens
                    command = command.split("@")[0]
                else:
                    command, *prefix = self.prompts.pop(user, "/help").split()
                    args = prefix + tokens
            reply = session.execute(command, args, user)
        except KeyError:
            reply = "سرور، تانل، نسخهٔ قبلی یا لایسنس پیدا نشد."
        except (RuntimeError, ValueError) as error:
            reply = str(error)
        except (OSError, subprocess.SubprocessError):
            reply = "عملیات ناموفق بود. وضعیت شبکه و سرویس را بررسی کنید."
        if reply is None:
            return True
        try:
            self.tg.send(user, reply, markup if not session.server_form else None)
        except RuntimeError:
            logging.warning("Could not send command response")
        return False


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
