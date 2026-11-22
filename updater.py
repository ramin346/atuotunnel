"""Fast-forward the managed checkout after checking the incoming Python files."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
ORIGIN = "https://github.com/ramin346/atuotunnel.git"
FILES = ("bot.py", "manager.py", "remote.py", "updater.py")


def git(*args, check=True):
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True,
        timeout=40, check=check, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def update():
    if git("symbolic-ref", "--short", "HEAD").stdout.strip() != "main":
        raise ValueError("به‌روزرسانی فقط برای نصب روی شاخهٔ main فعال است.")
    if git("status", "--porcelain", "--untracked-files=no").stdout:
        raise ValueError("نسخهٔ نصب‌شده تغییرات محلی دارد.")
    if git("remote", "get-url", "origin").stdout.strip() != ORIGIN:
        raise ValueError("آدرس origin با مخزن رسمی ربات مطابقت ندارد.")
    git("fetch", "--no-tags", "origin", "refs/heads/main")
    before = git("rev-parse", "HEAD").stdout.strip()
    after = git("rev-parse", "FETCH_HEAD").stdout.strip()
    if before == after:
        return None
    if git("merge-base", "--is-ancestor", before, after, check=False).returncode:
        raise ValueError("تاریخچهٔ شاخهٔ main با نسخهٔ نصب‌شده سازگار نیست.")
    with tempfile.TemporaryDirectory() as folder:
        for filename in FILES:
            source = git("show", f"{after}:{filename}").stdout
            try:
                compile(source, filename, "exec")
            except SyntaxError as error:
                raise ValueError(f"نسخهٔ جدید در فایل {filename} خطای پایتون دارد.") from error
            (Path(folder) / filename).write_text(source)
        subprocess.run(
            ["python3", "-B", "-c", "import bot, manager, remote, updater"],
            cwd=folder, check=True, capture_output=True, timeout=10,
        )
    git("-c", "core.hooksPath=/dev/null", "merge", "--ff-only", after)
    return after[:12]
