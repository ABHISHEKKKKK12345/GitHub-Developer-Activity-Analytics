"""
GitHub Developer Activity Analytics  v1.0.0
=========================================
Author  : Abhishek Srivastava
Project : GitHub Developer Activity Analytics

Counts active developers and commits, and measures commit-activity duration, for
  * a GitHub user         https://github.com/username
  * an organization       https://github.com/orgs/name/repositories  or  https://github.com/name
  * one repository        https://github.com/owner/repo  (also /tree/main, .git, ssh form)

Architecture
  GitHub REST API : account-type detection, repository DISCOVERY and metadata only.
  Git CLI         : clone/fetch and commit-HISTORY extraction (reachable refs only).
  pandas          : identity normalisation and statistics.
  openpyxl        : reconciled Excel workbook.

Requirements (running from source): Python 3.9+, pandas, requests, openpyxl, and Git 2.31+
(a bundled PortableGit is used automatically when present, otherwise Git on PATH).

Command line (from source)
  * python GitHub_Developer_Analytics.py                           start the GUI
  * python GitHub_Developer_Analytics.py --version                 show the version
  * python GitHub_Developer_Analytics.py --self-test [--online]    verify the runtime; the
    result is written to the 'logs' folder
  * python GitHub_Developer_Analytics.py --emit-build-files         optional developer helper:
    writes a PyInstaller .spec and version resource (no executable is distributed with the
    source repository)
"""
from __future__ import annotations

import errno
import importlib
import json
import logging
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests

try:                                    # Tk is only needed for the GUI
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:                     # pragma: no cover
    tk = messagebox = ttk = None

# ============================================================
# 1. CONFIGURATION
# ============================================================
APP_TITLE = "GitHub Developer Activity Analytics"
APP_VERSION = "1.0.0"
GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = (10, 30)              # connect, read (seconds)
PER_PAGE = 100
MAX_PAGES = 500                         # safety guard against endless pagination
MAX_API_RETRIES = 4                     # exponential back-off: 2,4,8,16 s
MAX_GIT_RETRIES = 2
MAX_PARALLEL_REPOS = 4                  # repositories cloned/read concurrently
MAX_RATE_LIMIT_WAIT = 120               # never sleep longer than this for a rate limit
DEFAULT_DAYS = 90
MAX_DAYS = 3650
INACTIVE_MARGIN_DAYS = 2
MIN_FREE_BYTES = 300 * 1024 * 1024
MAX_EXCEL_DATA_ROWS = 1_000_000         # Excel hard limit is 1,048,576 rows / sheet
DATE_FMT = "yyyy-mm-dd hh:mm:ss"
FONT_NAME = "Arial"

APP_FOLDER_NAME = "GitHub_Developer_Activity_Analytics"
FROZEN = bool(getattr(sys, "frozen", False))        # True inside a PyInstaller build


def _is_windows():
    return sys.platform.startswith("win")


def _writable_dir(path):
    """True if the folder exists (or can be created) and a file can be written in it."""
    try:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=str(path)):
            pass
        return True
    except (OSError, ValueError):
        return False


def _local_data_root():
    """Per-user, always-writable application folder (e.g. %LOCALAPPDATA%\\<app>)."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        try:
            home = Path.home()
        except RuntimeError:
            home = Path(tempfile.gettempdir())
        base = home / "AppData" / "Local" if _is_windows() else home / ".local" / "share"
    return Path(base) / APP_FOLDER_NAME


def _pick_writable(*candidates):
    for candidate in candidates:
        if candidate and _writable_dir(candidate):
            return Path(candidate)
    return Path(tempfile.gettempdir()) / APP_FOLDER_NAME


# Read-only bundled resources (PortableGit, certificates) vs. writable user data are kept
# apart.  Inside a PyInstaller build RESOURCE_DIR is sys._MEIPASS (the "_internal" folder
# for onedir builds, the temporary extraction folder for onefile builds); it is never
# written to.  Reports/logs go beside the EXE (portable, easy to find) and fall back to
# %LOCALAPPDATA% when that folder is read-only (e.g. extracted below "Program Files").
if FROZEN:
    APP_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
else:
    APP_DIR = RESOURCE_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR                                   # backward-compatible name
USER_DATA_DIR = _pick_writable(APP_DIR, _local_data_root())
# Git clones are a large, disposable cache: keep them in a short local path (avoids
# OneDrive syncing and Windows path-length problems) when running as a packaged app.
CACHE_DIR = _pick_writable(_local_data_root(), APP_DIR) if FROZEN else USER_DATA_DIR
REPOSITORIES_DIR = CACHE_DIR / "repositories"
OUTPUT_DIR = USER_DATA_DIR / "output"
LOGS_DIR = USER_DATA_DIR / "logs"
ALIASES_FILE = (APP_DIR / "identity_aliases.json"
                if (APP_DIR / "identity_aliases.json").exists()
                else USER_DATA_DIR / "identity_aliases.json")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

STATUS_ANALYSED = "Analysed"
STATUS_EXCLUDED = "Excluded"
STATUS_SKIPPED = "Skipped"
STATUS_FAILED = "Failed"
STATUS_CANCELLED = "Cancelled / Not Processed"
ALL_STATUSES = (STATUS_ANALYSED, STATUS_EXCLUDED, STATUS_SKIPPED,
                STATUS_FAILED, STATUS_CANCELLED)

ILLEGAL_XLSX_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
NOREPLY_RE = re.compile(r"^(?:\d+\+)?([^@\s]+)@users\.noreply\.github\.com$", re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
BOT_RE = re.compile(r"(\[bot\]|^dependabot|^renovate|^github-actions|"
                    r"^greenkeeper|^snyk-bot|-bot$)", re.I)
HASH_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")          # account names
# Repository names may START with a dot (e.g. the special ".github" repository);
# only "." and ".." are impossible.
REPO_NAME_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9_.-]{1,100}$")

file_logger = logging.getLogger("gh_activity")
file_logger.setLevel(logging.INFO)
file_logger.addHandler(logging.NullHandler())
_logging_ready = False


_log_folder = None


def setup_file_logging(logs_dir=None):
    """Attach a timestamped session log file (idempotent, never raises).
    Falls back to the system temp folder when the log folder is not writable."""
    global _logging_ready, _log_folder
    if _logging_ready:
        return
    candidates = [Path(logs_dir or LOGS_DIR),
                  Path(tempfile.gettempdir()) / APP_FOLDER_NAME / "logs"]
    for folder in candidates:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(
                folder / f"session_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            file_logger.addHandler(handler)
            _logging_ready = True
            _log_folder = folder
            file_logger.info("%s v%s started (frozen=%s, python %s, %s)", APP_TITLE,
                             APP_VERSION, FROZEN, sys.version.split()[0], sys.platform)
            return
        except OSError:
            continue


# ============================================================
# 2. SECURITY, ERRORS, SMALL HELPERS
# ============================================================
def redact(text, secrets=()):
    """Remove tokens, auth headers and URL credentials from any text."""
    text = str(text)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    text = re.sub(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}", "***", text)
    text = re.sub(r"(?i)(authorization:\s*)(?:basic|bearer|token)?\s*\S+", r"\1***", text)
    text = re.sub(r"(?i)\b(Basic|Bearer)\s+[A-Za-z0-9+/=._\-]{16,}", r"\1 ***", text)
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1***@", text)
    return text


class CancelledError(Exception):
    """Raised when the user cancels."""


class AppError(Exception):
    """Classified, user-presentable failure."""

    def __init__(self, message, category="Unexpected error", hint="",
                 transient=False, retries=0, stage=""):
        super().__init__(message)
        self.category, self.hint = category, hint
        self.transient, self.retries, self.stage = transient, retries, stage


class EmptyRepository(Exception):
    """Repository contains no commits."""


def force_rmtree(path):
    """rmtree that also removes read-only files (Git objects on Windows)."""
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    for attempt in range(4):             # antivirus/indexers may briefly hold files open
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=onerror)
            else:
                shutil.rmtree(path, onerror=onerror)
        except FileNotFoundError:
            return
        if not Path(path).exists():
            return
        time.sleep(0.5 * (attempt + 1))


def is_file_lock_error(exc):
    """True for 'file in use' / 'access denied' errors (Windows sharing violations)."""
    if not isinstance(exc, OSError):
        return False
    return (isinstance(exc, PermissionError)
            or getattr(exc, "winerror", None) in (5, 32, 33)
            or exc.errno in (errno.EACCES, errno.EPERM, errno.EBUSY))


def open_path(path):
    path = str(path)
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)                       # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        webbrowser.open(Path(path).as_uri())


# ============================================================
# 2b. GIT RUNTIME (bundled PortableGit first, system Git as fallback)
# ============================================================
GIT_ARCH_DIRS = ("ucrt64", "mingw64", "clang64", "clangarm64", "mingw32")
_git_runtime = None
_git_lock = threading.Lock()


def bundled_git_roots():
    """Candidate PortableGit folders, most specific first (onedir _internal, then beside
    the EXE, then beside the script when running from source)."""
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "PortableGit")
    roots += [APP_DIR / "PortableGit", APP_DIR / "_internal" / "PortableGit"]
    unique = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def inspect_portable_git(root):
    """Validate one PortableGit folder.  Returns (runtime_dict, fatal_problems).
    Recent Git for Windows uses 'ucrt64\\', older releases 'mingw64\\'; both are handled."""
    root = Path(root)
    if not root.is_dir():
        return None, [f"folder not found: {root}"]
    arch = next((a for a in GIT_ARCH_DIRS if (root / a / "bin" / "git.exe").is_file()), None)
    if arch is None:
        return None, ["no <arch>\\bin\\git.exe found (unrecognised PortableGit layout)"]
    exe = root / arch / "bin" / "git.exe"
    exec_path = root / arch / "libexec" / "git-core"
    problems, warnings = [], []
    if not (exec_path / "git-remote-https.exe").is_file():
        problems.append(f"{arch}\\libexec\\git-core\\git-remote-https.exe is missing "
                        "(HTTPS clone/fetch cannot work)")
    if not (root / arch / "etc" / "ssl" / "certs" / "ca-bundle.crt").is_file():
        warnings.append("CA certificate bundle not found inside PortableGit")
    if (root / "post-install.bat").exists():
        warnings.append("PortableGit post-install.bat was never run")
    if problems:
        return None, problems
    path_dirs = [d for d in (root / arch / "bin", root / "usr" / "bin") if d.is_dir()]
    return {"source": "bundled", "exe": exe, "root": root, "path_dirs": path_dirs,
            "exec_path": exec_path, "warnings": warnings}, []


def resolve_git(force=False):
    """Locate Git once.  Order: bundled PortableGit (Windows) -> system Git on PATH.
    The returned dict always has 'source' = 'bundled' | 'system' | 'missing'."""
    global _git_runtime
    with _git_lock:
        if _git_runtime is not None and not force:
            return _git_runtime
        notes, runtime = [], None
        if _is_windows():
            for root in bundled_git_roots():
                candidate, problems = inspect_portable_git(root)
                if candidate:
                    runtime = candidate
                    break
                if root.exists():
                    notes.append(f"Bundled Git at {root} is unusable: {'; '.join(problems)}")
        if runtime is None:
            found = shutil.which("git")
            if found:
                runtime = {"source": "system", "exe": Path(found), "root": None,
                           "path_dirs": [], "exec_path": None, "warnings": []}
            else:
                runtime = {"source": "missing", "exe": None, "root": None,
                           "path_dirs": [], "exec_path": None, "warnings": []}
        runtime["notes"] = notes + list(runtime.get("warnings", []))
        _git_runtime = runtime
        return runtime


def describe_git_runtime(runtime=None):
    runtime = runtime or resolve_git()
    if runtime["source"] == "bundled":
        text = f"bundled PortableGit ({runtime['exe']})"
    elif runtime["source"] == "system":
        text = f"system Git ({runtime['exe']})"
    else:
        text = "NOT FOUND"
    return text + ("".join(f"; WARNING: {n}" for n in runtime.get("notes", [])))


def git_environment(runtime=None):
    """Environment for every Git subprocess.  For the bundled Git the DLL/helper folders
    are put first on PATH and GIT_EXEC_PATH points at its own helpers, so nothing is
    picked up from a Git installed on the host."""
    runtime = runtime or resolve_git()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
               LC_MESSAGES="C", LANGUAGE="C")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE"):
        env.pop(key, None)                  # never let a stray variable redirect Git
    if runtime["source"] == "bundled":
        env["PATH"] = os.pathsep.join([str(d) for d in runtime["path_dirs"]]
                                      + [env.get("PATH", "")])
        if runtime["exec_path"] and Path(runtime["exec_path"]).is_dir():
            env["GIT_EXEC_PATH"] = str(runtime["exec_path"])
    return env


def kill_process_tree(proc):
    """Stop a process and its children (git spawns git-remote-https etc.)."""
    if proc.poll() is not None:
        return
    if _is_windows():
        try:
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            subprocess.run([str(taskkill), "/PID", str(proc.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                           timeout=15, check=False)
        except Exception:                                    # noqa: BLE001
            pass
    else:
        try:                                    # POSIX: Git was started as a group leader
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def reap_process(proc):
    """Collect a killed process's output without ever blocking the app indefinitely."""
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except Exception:                                # noqa: BLE001
                pass


def safe_print(text):
    """print() that cannot fail in a windowed build (where sys.stdout is None)."""
    try:
        stream = sys.stdout or sys.__stdout__
        if stream:
            stream.write(str(text) + "\n")
            stream.flush()
    except Exception:                                        # noqa: BLE001
        pass


def show_message(title, message, error=False):
    """Message box that works before Tk exists and in windowed builds."""
    message = str(message)[:3000]
    if _is_windows():
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, str(title),
                                             0x10 if error else 0x40)   # ICONERROR / ICONINFO
            return
        except Exception:                                    # noqa: BLE001
            pass
    if tk is not None and messagebox is not None:
        try:
            root = tk.Tk()
            root.withdraw()
            (messagebox.showerror if error else messagebox.showinfo)(title, message, parent=root)
            root.destroy()
            return
        except Exception:                                    # noqa: BLE001
            pass
    safe_print(f"{title}: {message}")


def install_exception_hooks():
    """Make uncaught errors visible and diagnosable in a console-less (windowed) EXE."""
    def handle(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        setup_file_logging()
        text = redact("".join(traceback.format_exception(exc_type, exc, tb)))
        file_logger.critical(text)
        show_message(f"{APP_TITLE} v{APP_VERSION} - Unexpected error",
                     f"{redact(exc)}\n\nDetails were written to:\n{_log_folder or LOGS_DIR}",
                     error=True)
    sys.excepthook = handle
    threading.excepthook = lambda args: handle(args.exc_type, args.exc_value, args.exc_traceback)



def clean_text(value, limit=32000):
    """Strip characters Excel rejects and keep within the 32,767 cell limit."""
    return ILLEGAL_XLSX_CHARS.sub("", str(value))[:limit]


def to_utc(value):
    """ISO-8601 string -> aware UTC datetime. Raises ValueError/OverflowError."""
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_api_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def normalise_identity(name, email, aliases=None):
    """Return (identity_key, method).

    Order of trust: GitHub noreply login > e-mail address > display name.
    Different e-mail addresses are NEVER merged automatically and display names
    are never used when an e-mail exists. Merges can be requested explicitly with
    identity_aliases.json ({"email:old@x.com": "github:login"}).
    """
    email = (email or "").strip().lower()
    name = " ".join((name or "").split())
    match = NOREPLY_RE.match(email)
    if match:
        key, method = f"github:{match.group(1).lower()}", "GitHub noreply (login)"
    elif EMAIL_RE.match(email):
        key, method = f"email:{email}", "E-mail address"
    elif name:
        key, method = f"name:{name.lower()}", "Display name only (no valid e-mail)"
    else:
        key, method = "unknown:(no author metadata)", "Unknown (no author metadata)"
    if aliases and key in aliases:
        key, method = aliases[key], "Alias mapping"
    return key, method


def is_bot(name, email):
    local = (email or "").split("@")[0]
    return bool(BOT_RE.search(name or "") or BOT_RE.search(local))


def classify_git_error(text, stage):
    """Map Git stderr to an AppError with category, hint and retry-ability."""
    low = text.lower()
    rules = [
        (("no space left", "disk full", "not enough space"), "Disk space", False,
         "Free disk space (or point the app at a larger drive) and retry."),
        (("permission denied", "access is denied", "operation not permitted"),
         "Filesystem permission", False,
         "Check write permission for the 'repositories' folder; close programs locking it."),
        (("dubious ownership",), "Git safe.directory", False,
         "Run the app as the user who owns the folder."),
        (("ssl certificate problem", "certificate verify failed", "schannel"),
         "SSL certificate", False,
         "A proxy/antivirus is intercepting HTTPS. Install the corporate CA in Git."),
        (("403", "access denied"), "Permission", False,
         "Token lacks access (needs repo read / SSO authorisation for the organisation)."),
        (("401", "authentication failed", "invalid username or password"),
         "Authentication", False, "Provide a valid GitHub token with read access."),
        (("could not read username", "terminal prompts disabled", "repository not found",
          "not found"), "Not found / no access", False,
         "Repository is private, renamed or deleted. Provide a token with read access."),
        (("not a git repository", "bad object", "corrupt", "loose object",
          "index file", "unable to read", "missing object", "fatal: bad"),
         "Corrupted local repository", False,
         "The local copy is damaged; the app re-clones it automatically."),
        (("filter", "partial clone"), "Partial clone unsupported", False, ""),
        (("502", "503", "504", "500 "), "Server error", True,
         "GitHub had a temporary problem. Retry later."),
        (("could not resolve host", "unable to access", "connection", "timed out",
          "early eof", "rpc failed", "unexpected disconnect", "gnutls", "proxy",
          "network is unreachable", "remote end hung up"), "Network", True,
         "Check internet connection, DNS, proxy or firewall."),
    ]
    for needles, category, transient, hint in rules:
        if any(n in low for n in needles):
            if category == "Partial clone unsupported" and not any(
                    k in low for k in ("not support", "unrecognized", "invalid filter",
                                       "does not support")):
                continue
            return AppError(text.strip()[:500] or category, category, hint,
                            transient, stage=stage)
    return AppError(text.strip()[:500] or "Unknown Git error.", "Git error",
                    "See the log for the full Git message.", False, stage=stage)


# ============================================================
# 3. ANALYSER  (no GUI code)
# ============================================================
COMMIT_COLS = [
    "Repository Name", "Repository Full Name", "Developer Name", "Developer Email",
    "Author Date (UTC)", "Committer Name", "Committer Email", "Committer Date (UTC)",
    "Date Used (UTC)", "Commit Hash", "Commit Message",
    "Normalised Developer Identity", "Identity Method"]
STATUS_COLS = [
    "Repository Name", "Repository Full Name", "Repository URL", "Visibility",
    "Fork Status", "Archived Status", "Disabled Status", "Last Push (UTC)",
    "Commits Read (all history)", "Commits Found", "Excluded: Bots",
    "Excluded: Merges", "Excluded: Outside Period",
    "Excluded: Invalid/Duplicate Records", "Processing Status", "Reason"]
ERROR_COLS = ["Timestamp (UTC)", "Repository", "Processing Stage", "Error Category",
              "Error Message", "Retry Count", "Final Status",
              "Recommended Corrective Action"]
ACTIVE_DEV_COLS = [
    "Developer Name", "Developer Email", "Total Commits", "Repository Count",
    "Repository Names", "First Commit Date (UTC)", "Latest Commit Date (UTC)",
    "Commit Activity Duration (Days)", "Active Days", "Share of Total Commits (%)"]
DEV_SUMMARY_COLS = [
    "Rank", "Developer Name", "Developer Email", "Normalised Developer Identity",
    "Identity Method", "Total Commits", "Share of Total Commits (%)",
    "Repository Count", "Repository Names", "First Commit Date (UTC)",
    "Latest Commit Date (UTC)", "Commit Activity Duration (Days)", "Active Days",
    "Avg Commits per Active Day", "Name Variants", "Email Variants"]
REPO_SUMMARY_COLS = [
    "Repository Name", "Repository Full Name", "Total Commits", "Unique Developers",
    "First Commit Date (UTC)", "Latest Commit Date (UTC)",
    "Commit Activity Duration (Days)", "Active Days", "Share of Total Commits (%)"]
INTEGRITY_COLS = ["Check", "Expected", "Actual", "Result", "Detail"]


class GitHubAnalyzer:

    def __init__(self, days=DEFAULT_DAYS, token="", include_forks=False,
                 include_archived=True, include_disabled=False, exclude_bots=False,
                 exclude_merges=False, skip_inactive=False, date_basis="author",
                 log_callback=None, progress_callback=None, cancel_event=None,
                 session=None, now=None, repos_dir=None, output_dir=None,
                 aliases=None, aliases_file=None):
        if not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
            raise ValueError(f"Reporting period must be a whole number of days "
                             f"between 1 and {MAX_DAYS}.")
        if date_basis not in ("author", "committer"):
            raise ValueError("date_basis must be 'author' or 'committer'.")
        self.days, self.date_basis = days, date_basis
        self.token = (token or "").strip()
        self.include_forks, self.include_archived = include_forks, include_archived
        self.include_disabled = include_disabled
        self.exclude_bots, self.exclude_merges = exclude_bots, exclude_merges
        self.skip_inactive = skip_inactive
        self.log = log_callback or (lambda _m: None)
        self.progress = progress_callback or (lambda _c, _t: None)
        self.cancel_event = cancel_event or threading.Event()
        self.repos_dir = Path(repos_dir) if repos_dir else REPOSITORIES_DIR
        self.output_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        self.warnings = []                  # must exist before _load_aliases can warn
        self._tls = threading.local()       # per-worker-thread log prefix
        self._parallel = False
        self.aliases = self._load_aliases(aliases, aliases_file)

        self.end_date = (now or datetime.now(timezone.utc)).replace(microsecond=0)
        self.start_date = self.end_date - timedelta(days=days)
        self.run_id = self.end_date.astimezone().strftime("%Y%m%d_%H%M%S")

        self.session = session or self._make_session()
        self.git_version = None
        self.commit_records, self.repo_rows = [], []
        self.processing_errors = []
        self.account = None
        self.discovery_complete = True
        self.api_duplicates = 0
        self.source_url, self.is_single_repo = "", False
        self.stats = {}

    # ---------------- setup helpers ----------------
    def _make_session(self):
        session = requests.Session()
        session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GitHub-Developer-Activity-Analytics"})
        if self.token:
            session.headers["Authorization"] = f"Bearer {self.token}"
        return session

    def _load_aliases(self, aliases, aliases_file):
        data = dict(aliases or {})
        path = Path(aliases_file) if aliases_file else ALIASES_FILE
        if not aliases and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                self.warnings.append(f"Could not read identity alias file {path}; ignored.")
        return {str(k).strip().lower(): str(v).strip().lower() for k, v in data.items()}

    def redact(self, text):
        return redact(text, (self.token,))

    def log_message(self, message):
        message = self.redact(message)
        file_logger.info(message)
        prefix = getattr(self._tls, "repo", "")
        if prefix and message:
            message = f"[{prefix}] {message}"
        self.log(message)

    def check_cancelled(self):
        if self.cancel_event.is_set():
            raise CancelledError("Cancelled by user.")

    def sleep(self, seconds):
        """Cancellation-aware sleep."""
        self.cancel_event.wait(seconds)
        self.check_cancelled()

    def add_error(self, repository, stage, category, message, retries=0,
                  final_status="Failed", action=""):
        self.processing_errors.append({
            "Timestamp (UTC)": datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
            "Repository": repository, "Processing Stage": stage,
            "Error Category": category, "Error Message": self.redact(message),
            "Retry Count": retries, "Final Status": final_status,
            "Recommended Corrective Action": action})

    # ---------------- URL parsing ----------------
    @staticmethod
    def parse_github_url(github_url):
        github_url = (github_url or "").strip()
        if not github_url:
            raise ValueError("Please enter a GitHub URL.")
        ssh = re.match(r"^git@github\.com:(.+)$", github_url, re.I)
        if ssh:
            github_url = "https://github.com/" + ssh.group(1)
        if not re.match(r"^https?://", github_url, re.I):
            github_url = "https://" + github_url
        parsed = urlparse(github_url)
        if parsed.username or parsed.password:
            raise ValueError("Do not put credentials in the URL. Use the token field.")
        if (parsed.hostname or "").lower() not in {"github.com", "www.github.com"}:
            raise ValueError("Invalid URL. Please enter a github.com URL.")
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if not parts:
            raise ValueError("Invalid GitHub URL: no user, organization or repository found.")
        if parts[0].lower() in ("orgs", "users"):
            if len(parts) < 2:
                raise ValueError("Invalid GitHub URL: missing account name.")
            owner, repo, kind = parts[1], None, "profile"
        elif len(parts) == 1:
            owner, repo, kind = parts[0], None, "profile"
        else:
            owner, repo, kind = parts[0], parts[1], "repository"
            if repo.lower().endswith(".git"):
                repo = repo[:-4]
        if not NAME_RE.match(owner) or (repo is not None and not REPO_NAME_RE.match(repo)):
            raise ValueError("Invalid GitHub account or repository name in URL.")
        return {"type": kind, "owner": owner, "repository": repo}

    # ---------------- GitHub API ----------------
    def _http_error(self, resp, endpoint):
        """Return (AppError, wait_seconds_or_None) for an HTTP >= 300 response."""
        code = resp.status_code
        headers = {str(k).lower(): v for k, v in dict(resp.headers or {}).items()}
        try:
            body = resp.json()
            message = body.get("message", "") if isinstance(body, dict) else ""
        except Exception:
            message = (getattr(resp, "text", "") or "")[:200]
        message = self.redact(message)
        low = message.lower()
        stage = "GitHub API"

        if code == 401:
            return AppError("GitHub rejected the credentials (token invalid or expired).",
                            "Authentication", "Create a new token and paste it again "
                            "(or leave the field empty for public data).", stage=stage), None
        if code in (403, 429):
            retry_after = headers.get("retry-after", "")
            if retry_after.isdigit() or "secondary rate limit" in low or "abuse" in low:
                wait = int(retry_after) if retry_after.isdigit() else 60
                err = AppError(f"GitHub secondary rate limit hit (wait {wait}s).",
                               "Rate limit (secondary)", "Wait a few minutes; use a token.",
                               transient=wait <= MAX_RATE_LIMIT_WAIT, stage=stage)
                return err, wait
            if headers.get("x-ratelimit-remaining") == "0" or code == 429 or "rate limit" in low:
                reset = headers.get("x-ratelimit-reset", "")
                wait = None
                when = ""
                if reset.isdigit():
                    wait = max(int(reset) - time.time(), 0) + 1
                    when = " Limit resets at " + datetime.fromtimestamp(
                        int(reset), timezone.utc).strftime("%H:%M:%S UTC") + "."
                ok = wait is not None and wait <= MAX_RATE_LIMIT_WAIT
                return AppError("GitHub API rate limit exceeded." + when,
                                "Rate limit (primary)",
                                "Use a token (5,000 requests/hour) or retry after the reset.",
                                transient=ok, stage=stage), (wait if ok else None)
            if headers.get("x-github-sso"):
                return AppError("Organization requires SAML SSO authorisation for this token.",
                                "Organization SSO", "Authorise the token for the "
                                "organisation (Token settings > Configure SSO).",
                                stage=stage), None
            return AppError("GitHub API access denied (HTTP 403)"
                            + (f": {message}" if message else "."), "Permission",
                            "Check token scopes/permissions for this account.", stage=stage), None
        if code == 404:
            return AppError(f"Not found: {endpoint}. Misspelled, or private and the token "
                            "lacks access.", "Not found / no access",
                            "Verify the URL and that the token can read it.", stage=stage), None
        if code == 451:
            return AppError("Repository unavailable for legal reasons (HTTP 451).",
                            "Unavailable", "Cannot be analysed.", stage=stage), None
        if code in (400, 409, 422):
            return AppError(f"GitHub rejected the request (HTTP {code}): {message}",
                            f"HTTP {code}", "See message; request parameters/state invalid.",
                            stage=stage), None
        if code == 408 or code >= 500:
            return AppError(f"GitHub server error (HTTP {code}).", "Server error",
                            "Temporary GitHub problem; retry later.", transient=True,
                            stage=stage), None
        return AppError(f"Unexpected HTTP status {code}: {message}", f"HTTP {code}",
                        "", stage=stage), None

    def _api_request(self, endpoint, params=None):
        """GET with bounded retries. Returns (json, lower-cased headers)."""
        url = f"{GITHUB_API}{endpoint}"
        retries = 0
        while True:
            self.check_cancelled()
            err, wait, result = None, None, None
            try:
                # HTTPS certificate verification is enabled by default.
                # If verification fails, check the server certificate and trusted CA configuration.
                # If a custom CA is required, obtain a trusted PEM-format CA bundle.
                # Set REQUESTS_CA_BUNDLE to its full path, or pass verify="/actual/path/to/ca-bundle.pem".
                # Do not disable certificate verification in shared or production code.
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except requests.exceptions.SSLError:
                err = AppError("SSL error contacting GitHub; a proxy or antivirus may be "
                               "intercepting HTTPS.", "SSL certificate",
                               "Install the corporate root CA or disable HTTPS inspection "
                               "for api.github.com.", stage="GitHub API")
            except requests.exceptions.Timeout:
                err = AppError("GitHub request timed out.", "Timeout",
                               "Check your connection and retry.", True, stage="GitHub API")
            except requests.exceptions.ProxyError:
                err = AppError("Proxy error while contacting GitHub.", "Network (proxy)",
                               "Check proxy settings.", True, stage="GitHub API")
            except requests.exceptions.ConnectionError:
                err = AppError("Cannot reach GitHub (offline, DNS failure or firewall).",
                               "Network", "Check internet connection, DNS, proxy, firewall.",
                               True, stage="GitHub API")
            except requests.exceptions.RequestException as exc:
                err = AppError(f"Network error: {self.redact(exc)}", "Network",
                               "Check connection.", False, stage="GitHub API")
            else:
                if 200 <= resp.status_code < 300:
                    try:
                        result = (resp.json(),
                                  {str(k).lower(): v for k, v in dict(resp.headers or {}).items()})
                    except ValueError:
                        err = AppError("GitHub returned malformed JSON.", "Malformed response",
                                       "Retry; if persistent a proxy may be altering responses.",
                                       True, stage="GitHub API")
                else:
                    err, wait = self._http_error(resp, endpoint)
            if result is not None:
                return result
            if err.transient and retries < MAX_API_RETRIES:
                retries += 1
                delay = wait if wait is not None else min(2 ** retries, 30)
                self.log_message(f"  {err.category}: retry {retries}/{MAX_API_RETRIES} "
                                 f"in {delay:.0f}s ...")
                self.sleep(delay)
                continue
            err.retries = retries
            raise err

    def api_get(self, endpoint, params=None):
        return self._api_request(endpoint, params)[0]

    def get_all_pages(self, endpoint, params=None):
        """Follow pagination. Returns (items, complete, error). A failure after
        page 1 keeps what was retrieved and flags the result as incomplete."""
        items, page, error = [], 1, None
        while page <= MAX_PAGES:
            self.check_cancelled()
            query = dict(params or {})
            query.update({"per_page": PER_PAGE, "page": page})
            try:
                data, headers = self._api_request(endpoint, query)
                if not isinstance(data, list):
                    raise AppError("Unexpected API response structure (expected a list).",
                                   "Malformed response", "Retry later.", stage="GitHub API")
            except CancelledError:
                raise
            except AppError as exc:
                if page == 1:
                    raise
                error = exc
                break
            items.extend(data)
            self.log_message(f"  Retrieved page {page}: {len(data)} records "
                             f"({len(items)} so far).")
            link = headers.get("link", "")
            has_next = (bool(re.search(r'rel="next"', link)) if link
                        else len(data) >= PER_PAGE)
            if not data or not has_next:
                return items, True, None
            page += 1
        else:
            error = AppError(f"Pagination safety limit of {MAX_PAGES} pages reached.",
                             "Pagination", "Narrow the scope.", stage="GitHub API")
        return items, False, error

    @staticmethod
    def normalise_repo(raw):
        """Validated, uniform repository dict - or None if the record is unusable."""
        if not isinstance(raw, dict):
            return None
        name, full = raw.get("name"), raw.get("full_name")
        if not isinstance(name, str) or not isinstance(full, str) or "/" not in full:
            return None
        owner = (raw.get("owner") or {}).get("login") if isinstance(raw.get("owner"), dict) \
            else full.split("/")[0]
        if not isinstance(owner, str) or not NAME_RE.match(owner) \
                or not REPO_NAME_RE.match(name):
            return None
        private = bool(raw.get("private"))
        return {
            "id": raw.get("id"), "name": name, "full_name": full, "owner": owner,
            "clone_url": raw.get("clone_url") or f"https://github.com/{full}.git",
            "html_url": raw.get("html_url") or f"https://github.com/{full}",
            "private": private,
            "visibility": raw.get("visibility") or ("private" if private else "public"),
            "fork": bool(raw.get("fork")), "archived": bool(raw.get("archived")),
            "disabled": bool(raw.get("disabled")),
            "pushed_at": raw.get("pushed_at"), "size": raw.get("size")}

    def discover_repositories(self, owner):
        """Returns list of normalised repo dicts + list of invalid-record notes."""
        account = self.api_get(f"/users/{owner}")
        if not isinstance(account, dict) or account.get("type") not in ("User", "Organization"):
            raise AppError("Could not identify the GitHub account type.",
                           "Malformed response", "Verify the URL.", stage="Account lookup")
        login, kind = account.get("login") or owner, account["type"]
        self.account = {"type": kind, "login": login,
                        "public_repos": account.get("public_repos")}
        self.log_message(f"GitHub account identified: {login} ({kind})")

        # sort=full_name (stable). Sorting by 'updated' while paging lets pushes
        # made during the run reorder pages and lose/duplicate repositories.
        params = {"sort": "full_name", "direction": "asc"}
        if kind == "Organization":
            endpoint, params["type"] = f"/orgs/{login}/repos", "all"
        else:
            endpoint, params["type"] = f"/users/{login}/repos", "owner"
            if self.token:                              # include own private repos
                try:
                    me = self.api_get("/user")
                    if isinstance(me, dict) and str(me.get("login", "")).lower() == login.lower():
                        endpoint = "/user/repos"
                        params = {"sort": "full_name", "direction": "asc",
                                  "affiliation": "owner"}
                except AppError as exc:
                    self.log_message(f"  Could not verify token owner ({exc.category}); "
                                     "using public listing.")
        self.log_message("Discovering repositories through the GitHub API...")
        raw, complete, error = self.get_all_pages(endpoint, params)
        if not complete:
            self.discovery_complete = False
            self.add_error(login, "Repository discovery", error.category, str(error),
                           error.retries, "Discovery incomplete",
                           "Re-run the analysis; results contain only repositories "
                           "retrieved before the interruption.")
            self.warnings.append("Repository discovery was interrupted; the list is incomplete.")

        repos, invalid, seen = [], [], set()
        for index, item in enumerate(raw, 1):
            repo = self.normalise_repo(item)
            if repo is None:
                hint = (f": {item['full_name']}" if isinstance(item, dict)
                        and isinstance(item.get("full_name"), str) else "")
                invalid.append(f"(invalid API record #{index}{clean_text(hint, 120)})")
                continue
            key = repo["full_name"].lower()
            if key in seen:
                self.api_duplicates += 1
                continue
            seen.add(key)
            repos.append(repo)
        self.log_message(f"Repositories discovered: {len(repos)}"
                         + (f" (+{len(invalid)} unreadable records)" if invalid else "")
                         + (f", {self.api_duplicates} duplicate API records removed"
                            if self.api_duplicates else ""))
        expected = account.get("public_repos")
        found = len(repos) + len(invalid)
        # Private repositories visible to the token are legitimately extra, so only
        # FEWER repositories than GitHub's public count indicates a possible loss.
        if complete and isinstance(expected, int) and found < expected:
            self.warnings.append(
                f"GitHub reports {expected} public repositories for '{login}' but the "
                f"API returned only {found} repositories in total.")
        return repos, invalid

    # ---------------- row bookkeeping ----------------
    @staticmethod
    def new_row(repo):
        return {
            "Repository Name": repo["name"], "Repository Full Name": repo["full_name"],
            "Repository URL": repo["html_url"], "Visibility": repo["visibility"],
            "Fork Status": "Yes" if repo["fork"] else "No",
            "Archived Status": "Yes" if repo["archived"] else "No",
            "Disabled Status": "Yes" if repo["disabled"] else "No",
            "Last Push (UTC)": parse_api_date(repo["pushed_at"]),
            "Commits Read (all history)": 0, "Commits Found": 0, "Excluded: Bots": 0,
            "Excluded: Merges": 0, "Excluded: Outside Period": 0,
            "Excluded: Invalid/Duplicate Records": 0,
            "Processing Status": STATUS_CANCELLED, "Reason": "Not processed.",
            "_repo": repo}

    def exclusion_reason(self, repo):
        why = []
        if repo["fork"] and not self.include_forks:
            why.append("Fork (forks not included)")
        if repo["archived"] and not self.include_archived:
            why.append("Archived (archived not included)")
        if repo["disabled"] and not self.include_disabled:
            why.append("Disabled (disabled not included)")
        return "; ".join(why)

    def is_inactive(self, repo):
        """True when the last push predates the window (so no in-window commit can
        exist). Margin protects against clock skew."""
        pushed = parse_api_date(repo.get("pushed_at"))
        if pushed is None:
            return False
        return pushed.replace(tzinfo=timezone.utc) < self.start_date - timedelta(
            days=INACTIVE_MARGIN_DAYS)

    # ---------------- Git ----------------
    def repository_folder(self, owner, repository):
        safe = re.sub(r"[^a-z0-9_.-]", "_", f"{owner}__{repository}".lower())
        return self.repos_dir / safe

    def _start_git(self, args, cwd=None, auth=False, stage="Git"):
        """Start a Git subprocess (stdout/stderr piped) and return the Popen object."""
        runtime = resolve_git()
        if runtime["source"] == "missing":
            raise AppError(
                "Git could not be found." if not FROZEN else
                "The bundled Git (PortableGit folder) is missing or incomplete.",
                "Git missing",
                "Extract the complete application ZIP again (do not move the EXE out of its "
                "folder) and keep antivirus from quarantining the PortableGit folder."
                if FROZEN else
                "Install Git from https://git-scm.com/downloads or place a PortableGit "
                "folder next to this script.", stage=stage)
        # gc.auto/maintenance off: no surprise background repack after clone/fetch;
        # showSignature off: never spawn gpg while reading history.
        cmd = [str(runtime["exe"]), "-c", "core.longpaths=true", "-c", "gc.auto=0",
               "-c", "maintenance.auto=false", "-c", "log.showSignature=false"]
        if auth:                       # network operation: abort completely stalled transfers
            cmd += ["-c", "http.lowSpeedLimit=1", "-c", "http.lowSpeedTime=180"]
        env = git_environment(runtime)
        if cwd is not None:
            cmd += ["-c", f"safe.directory={Path(cwd).as_posix()}", "-C", str(cwd)]
            # never let Git climb out of the clone into some parent repository
            env["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).resolve().parent)
        if auth and self.token:
            import base64
            raw = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env.update(GIT_CONFIG_COUNT="1",
                       GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0=f"Authorization: Basic {raw}")
        cmd += args
        try:
            # stdin=DEVNULL: a windowed EXE has no valid stdin handle to inherit.
            return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    errors="replace", env=env, creationflags=CREATE_NO_WINDOW,
                                    start_new_session=not _is_windows(), shell=False)
        except FileNotFoundError:
            raise AppError(f"Git executable not found: {runtime['exe']}", "Git missing",
                           "Extract the complete application ZIP again." if FROZEN else
                           "Install Git from https://git-scm.com/downloads", stage=stage)
        except OSError as exc:
            raise AppError(f"Could not start Git: {exc}", "Filesystem permission",
                           "Check permissions; security software may be blocking git.exe.",
                           stage=stage)

    def run_git(self, args, cwd=None, auth=False, timeout=None, stage="Git"):
        proc = self._start_git(args, cwd=cwd, auth=auth, stage=stage)
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if self.cancel_event.is_set():
                    kill_process_tree(proc)
                    reap_process(proc)
                    raise CancelledError("Cancelled by user.")
                if deadline and time.monotonic() > deadline:
                    kill_process_tree(proc)
                    reap_process(proc)
                    raise AppError(f"Git {args[0]} timed out after {timeout}s.", "Timeout",
                                   "Retry; for very large repositories the network is "
                                   "probably too slow.", True, stage=stage)
        if proc.returncode != 0:
            text = self.redact(err.strip() or out.strip() or "Unknown Git error.")
            raise classify_git_error(text, stage)
        return out

    def stream_git(self, args, cwd=None, timeout=None, stage="Git", chunk=262144):
        """Generator yielding stdout text in chunks, so a multi-million-commit history is
        processed with bounded memory.  Cancellation and the time-out are checked every
        0.5 s; stderr is drained on its own thread so Git can never block on a full pipe."""
        proc = self._start_git(args, cwd=cwd, stage=stage)
        out_q = queue.Queue(maxsize=8)
        stop = threading.Event()
        err_lines = []

        def pump_out():
            try:
                while not stop.is_set():
                    data = proc.stdout.read(chunk)
                    if not data:
                        break
                    while not stop.is_set():
                        try:
                            out_q.put(data, timeout=0.5)
                            break
                        except queue.Full:
                            continue
            except Exception:                                # noqa: BLE001
                pass
            finally:
                while not stop.is_set():
                    try:
                        out_q.put(None, timeout=0.5)
                        break
                    except queue.Full:
                        continue

        def pump_err():
            try:
                for line in proc.stderr:
                    if len(err_lines) < 100:
                        err_lines.append(line)
            except Exception:                                # noqa: BLE001
                pass

        t_out = threading.Thread(target=pump_out, daemon=True)
        t_err = threading.Thread(target=pump_err, daemon=True)
        t_out.start()
        t_err.start()
        deadline = time.monotonic() + timeout if timeout else None

        def check():
            if self.cancel_event.is_set():
                raise CancelledError("Cancelled by user.")
            if deadline and time.monotonic() > deadline:
                raise AppError(f"Git {args[0]} timed out after {timeout}s.", "Timeout",
                               "Retry; the repository may be very large.", True, stage=stage)
        try:
            while True:
                check()
                try:
                    data = out_q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if data is None:
                    break
                yield data
            while proc.poll() is None:
                check()
                self.cancel_event.wait(0.1)
            t_err.join(timeout=5)
            if proc.returncode != 0:
                text = self.redact("".join(err_lines).strip() or "Unknown Git error.")
                raise classify_git_error(text, stage)
        finally:
            stop.set()
            if proc.poll() is None:
                kill_process_tree(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            t_out.join(timeout=5)
            t_err.join(timeout=5)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:                            # noqa: BLE001
                    pass

    def git_retry(self, args, **kw):
        """run_git with bounded exponential-backoff retries for transient failures."""
        attempt = 0
        while True:
            try:
                return self.run_git(args, **kw)
            except AppError as exc:
                if exc.transient and attempt < MAX_GIT_RETRIES:
                    attempt += 1
                    self.log_message(f"  {exc.category}; Git retry {attempt}/{MAX_GIT_RETRIES}"
                                     f" in {2 ** attempt}s ...")
                    self.sleep(2 ** attempt)
                    continue
                exc.retries = attempt
                raise

    def check_disk_space(self):
        try:
            free = shutil.disk_usage(self.repos_dir).free
        except OSError:
            return
        if free < MIN_FREE_BYTES:
            raise AppError(f"Only {free // (1024 * 1024)} MB free disk space.", "Disk space",
                           "Free at least 300 MB (or move the app to a larger drive).",
                           stage="Clone/Fetch")

    def cleanup_stale_temp(self):
        """Remove abandoned '<repo>.cloning' folders left by a crashed/killed earlier run
        (only ones untouched for an hour, so a parallel run is never disturbed)."""
        try:
            if not self.repos_dir.is_dir():
                return
            for leftover in self.repos_dir.glob("*.cloning"):
                if leftover.is_dir() and time.time() - leftover.stat().st_mtime > 3600:
                    force_rmtree(leftover)
        except OSError:
            pass

    def move_into_place(self, source, destination, max_wait=45):
        """Rename a folder, retrying while Windows Defender / the search indexer / a
        syncing client still holds handles on the freshly written files (WinError 5/32).
        Last resort: copy the tree and delete the original."""
        deadline = time.monotonic() + max_wait
        attempt = 0
        while True:
            self.check_cancelled()
            try:
                os.replace(source, destination)
                return
            except OSError as exc:
                if not is_file_lock_error(exc):
                    raise
                attempt += 1
                if time.monotonic() > deadline:
                    break
                if attempt in (1, 5, 15):
                    self.log_message("  Folder is locked by another process (antivirus or "
                                     "indexer?); retrying ...")
                self.sleep(min(0.5 * attempt, 3))
        self.log_message("  Folder still locked; copying into place instead ...")
        try:
            if Path(destination).exists():
                force_rmtree(destination)
            shutil.copytree(source, destination)
        except OSError as exc:
            if Path(destination).exists():
                force_rmtree(destination)
            raise AppError(
                f"Could not move {Path(source).name} into place after {max_wait}s: "
                f"{self.redact(exc)}", "Antivirus / file lock",
                "Add the repository cache folder to your antivirus exclusions "
                f"({self.repos_dir}) and run again.", True, stage="Clone/Fetch")
        force_rmtree(source)

    def clone_repository(self, repo, destination):
        """Clone into '<dest>.cloning' and move into place only on success, so a
        failure never leaves (or destroys) a half-written repository folder.
        Only commit objects are needed to read history, so the lightest partial-clone
        filter (tree:0) is tried first, then blob:none, then a full clone."""
        tmp = destination.with_name(destination.name + ".cloning")
        if tmp.exists():
            force_rmtree(tmp)                 # stale temp from an interrupted run
        base = ["clone", "--quiet", "--no-checkout"]
        filters = ("--filter=tree:0", "--filter=blob:none", None)
        try:
            for position, flt in enumerate(filters):
                try:
                    self.git_retry(base + ([flt] if flt else []) + [repo["clone_url"], str(tmp)],
                                   auth=True, timeout=1800, stage="Clone")
                    break
                except AppError as exc:
                    if (position == len(filters) - 1
                            or exc.category not in ("Partial clone unsupported", "Git error")):
                        raise
                    self.log_message("Partial clone failed; retrying with a "
                                     + ("lighter-weight" if position == 0 else "full")
                                     + " clone.")
                    if tmp.exists():
                        force_rmtree(tmp)
            self.move_into_place(tmp, destination)
        except BaseException:
            if tmp.exists():
                force_rmtree(tmp)
            raise

    def verify_local_repository(self, path):
        out = self.run_git(["rev-parse", "--git-dir"], cwd=path, timeout=60,
                           stage="Verify local copy").strip()
        if out != ".git":
            raise AppError("Local folder is not a standalone Git repository.",
                           "Corrupted local repository", "", stage="Verify local copy")

    def reclone(self, repo, local):
        """Replace a corrupted clone; the old copy is restored if re-cloning fails."""
        self.log_message("Local copy is damaged; re-cloning...")
        backup = local.with_name(local.name + ".old")
        if backup.exists():
            force_rmtree(backup)
        self.move_into_place(local, backup)
        try:
            self.clone_repository(repo, local)
        except BaseException:
            if local.exists():
                force_rmtree(local)
            self.move_into_place(backup, local)
            raise
        force_rmtree(backup)

    def clone_or_update(self, repo):
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        local = self.repository_folder(repo["owner"], repo["name"])
        self.check_disk_space()
        if local.exists():
            if not (local / ".git").exists():
                raise AppError(f"Folder exists but is not a Git repository: {local}",
                               "Local repository", "Move or delete that folder and retry "
                               "(the app never deletes it for you).", stage="Clone/Fetch")
            self.log_message("Existing local copy found. Fetching latest changes...")
            try:
                self.verify_local_repository(local)
                self.git_retry(["fetch", "--all", "--prune", "--quiet"], cwd=local,
                               auth=True, timeout=900, stage="Fetch")
            except AppError as exc:
                if exc.category not in ("Corrupted local repository", "Git error"):
                    raise
                self.reclone(repo, local)
        else:
            self.log_message("Cloning history only (no files). Please wait...")
            self.clone_repository(repo, local)
        return local

    def iter_log_records(self, local, fmt):
        """Yield the raw '\\x1e'-terminated commit records of 'git log --all' while the
        output streams in (never holds the complete history in memory)."""
        buf = ""
        args = ["log", "--all", "--no-color", "--no-decorate", f"--format={fmt}"]
        for data in self.stream_git(args, cwd=local, timeout=1800, stage="Reading history"):
            buf += data
            parts = buf.split("\x1e")
            buf = parts.pop()
            yield from parts
        if buf.strip("\r\n"):
            yield buf

    def extract_commits(self, local, repo):
        """Read every commit reachable from fetched refs and apply all filters.
        Every record falls in exactly one bucket (qualifying / invalid / duplicate /
        outside period / bot / merge) so nothing disappears silently.
        Speed: timestamps are read as epoch seconds (integer compare, no ISO parsing)
        and identity/bot/clean-up work is cached per distinct author/committer."""
        full = repo["full_name"]
        self.log_message(f"Reading Git history for {full}...")
        refs = self.run_git(["for-each-ref", "--count=1"], cwd=local, timeout=120,
                            stage="Reading history")
        if not refs.strip():
            raise EmptyRepository()
        fmt = "%H%x1f%an%x1f%ae%x1f%at%x1f%cn%x1f%ce%x1f%ct%x1f%P%x1f%s%x1e"
        stats = {"read": 0, "qualifying": 0, "bots": 0, "merges": 0, "outside": 0,
                 "invalid": 0, "duplicate": 0, "missing_meta": 0}
        samples = {"invalid": [], "missing_meta": []}
        commits, seen = [], set()
        use_author = self.date_basis == "author"
        start_ts, end_ts = self.start_date.timestamp(), self.end_date.timestamp()
        author_cache, committer_cache = {}, {}
        repo_name = repo["name"]
        utc = timezone.utc
        for record in self.iter_log_records(local, fmt):
            record = record.strip("\r\n")
            if not record:
                continue
            stats["read"] += 1
            fields = record.split("\x1f")
            commit_hash = fields[0].strip() if fields else ""
            if len(fields) != 9 or not HASH_RE.match(commit_hash):
                stats["invalid"] += 1
                if len(samples["invalid"]) < 3:
                    samples["invalid"].append(commit_hash[:12] or "(no hash)")
                continue
            (_h, a_name, a_email, a_ts, c_name, c_email, c_ts, parents, subject) = fields
            try:
                a_val, c_val = int(a_ts), int(c_ts)
            except ValueError:
                stats["invalid"] += 1
                if len(samples["invalid"]) < 3:
                    samples["invalid"].append(commit_hash[:12])
                continue
            used = a_val if use_author else c_val
            if not (start_ts <= used <= end_ts):
                stats["outside"] += 1
                continue
            # (A commit cannot appear twice in one 'git log --all'; the duplicate guard is
            # kept for in-window commits only, so no hash set is held for old history.)
            if commit_hash in seen:
                stats["duplicate"] += 1
                continue
            ident = author_cache.get((a_name, a_email))
            if ident is None:
                key, method = normalise_identity(a_name, a_email, self.aliases)
                ident = (clean_text(a_name.strip()), clean_text(a_email.strip()), key, method,
                         is_bot(a_name, a_email), not a_name.strip() and not a_email.strip())
                author_cache[(a_name, a_email)] = ident
            if self.exclude_bots and ident[4]:
                stats["bots"] += 1
                continue
            if self.exclude_merges and len(parents.split()) > 1:
                stats["merges"] += 1
                continue
            try:
                author_dt = datetime.fromtimestamp(a_val, utc).replace(tzinfo=None)
                committer_dt = datetime.fromtimestamp(c_val, utc).replace(tzinfo=None)
            except (ValueError, OverflowError, OSError):
                stats["invalid"] += 1
                if len(samples["invalid"]) < 3:
                    samples["invalid"].append(commit_hash[:12])
                continue
            seen.add(commit_hash)
            if ident[5]:
                stats["missing_meta"] += 1
                if len(samples["missing_meta"]) < 3:
                    samples["missing_meta"].append(commit_hash[:12])
            committer = committer_cache.get((c_name, c_email))
            if committer is None:
                committer = (clean_text(c_name.strip()), clean_text(c_email.strip()))
                committer_cache[(c_name, c_email)] = committer
            commits.append((
                repo_name, full, ident[0], ident[1], author_dt, committer[0], committer[1],
                committer_dt, author_dt if use_author else committer_dt, commit_hash,
                clean_text(subject), ident[2], ident[3]))
            stats["qualifying"] += 1
        self.log_message(f"Found {stats['qualifying']} qualifying commits in {full} "
                         f"({stats['read']} read).")
        return commits, stats, samples

    # ---------------- per-repository processing ----------------
    def process_repository(self, row):
        repo, stage = row["_repo"], "Preparation"
        name = repo["full_name"]
        try:
            if not repo["pushed_at"] and not repo.get("size"):
                row.update({"Processing Status": STATUS_SKIPPED,
                            "Reason": "Empty repository (never pushed; no commits)."})
                return
            if self.skip_inactive and self.is_inactive(repo):
                row.update({"Processing Status": STATUS_SKIPPED,
                            "Reason": "No push within the reporting period "
                                      "('skip inactive' option is on)."})
                self.log_message("SKIPPED: no pushes in the reporting period.")
                return
            if repo["disabled"]:
                raise AppError("Repository is disabled on GitHub and cannot be cloned.",
                               "Disabled repository", "Re-enable it on GitHub or exclude it.",
                               stage="Preparation")
            stage = "Clone/Fetch"
            local = self.clone_or_update(repo)
            stage = "Reading history"
            commits, st, samples = self.extract_commits(local, repo)
            self.commit_records.extend(commits)
            row.update({
                "Commits Read (all history)": st["read"], "Commits Found": st["qualifying"],
                "Excluded: Bots": st["bots"], "Excluded: Merges": st["merges"],
                "Excluded: Outside Period": st["outside"],
                "Excluded: Invalid/Duplicate Records": st["invalid"] + st["duplicate"],
                "Processing Status": STATUS_ANALYSED,
                "Reason": "" if st["qualifying"] else "Analysed; no qualifying commits "
                                                      "in the reporting period."})
            for kind, label in (("invalid", "commit record(s) with missing hash/invalid "
                                             "timestamp/malformed fields (excluded)"),
                                ("missing_meta", "commit(s) with no author name or e-mail "
                                                 "(kept as 'unknown')")):
                if samples[kind] or (kind == "invalid" and st["invalid"]):
                    count = st["invalid"] if kind == "invalid" else st["missing_meta"]
                    self.add_error(name, "Commit parsing", "Data integrity",
                                   f"{count} {label}; e.g. {', '.join(samples[kind][:3])}",
                                   0, "Recorded (not fatal)",
                                   "Inspect these commits in the repository.")
            if st["duplicate"]:
                self.add_error(name, "Commit parsing", "Data integrity",
                               f"{st['duplicate']} duplicate commit hash(es) ignored.",
                               0, "Recorded (not fatal)", "None required.")
            self.log_message(f"SUCCESS: {name}")
        except CancelledError:
            row.update({"Processing Status": STATUS_CANCELLED,
                        "Reason": f"Cancelled during {stage}."})
            raise
        except EmptyRepository:
            row.update({"Processing Status": STATUS_SKIPPED,
                        "Reason": "Empty repository (no commits/refs)."})
            self.log_message(f"{name} is empty (no commits).")
        except AppError as exc:
            self._fail(row, name, exc.stage or stage, exc.category, str(exc),
                       exc.retries, exc.hint)
        except OSError as exc:
            file_logger.error(self.redact(traceback.format_exc()))
            if is_file_lock_error(exc):
                self._fail(row, name, stage, "Antivirus / file lock", f"{type(exc).__name__}: "
                           f"{exc}", 0, "Exclude the repository cache folder "
                           f"({self.repos_dir}) from antivirus/indexing and run again; "
                           "completed clones are reused.")
            else:
                self._fail(row, name, stage, "Filesystem", f"{type(exc).__name__}: {exc}",
                           0, "Check disk space, permissions and path length.")
        except Exception as exc:                    # unexpected bug - never lose the repo
            file_logger.error(self.redact(traceback.format_exc()))
            self._fail(row, name, stage, "Unexpected error",
                       f"{type(exc).__name__}: {exc}", 0, "See the session log for details.")

    def _run_repo_task(self, index, total, row):
        """One repository inside the worker pool. Returns True when cancellation
        prevented it from completing."""
        if self.cancel_event.is_set():
            return True
        self.log_message("")
        self.log_message("-" * 65)
        self.log_message(f"STEP {index} OF {total}: {row['Repository Full Name']}")
        if self._parallel:
            self._tls.repo = row["Repository Full Name"]
        try:
            self.process_repository(row)
            return False
        except CancelledError:
            return True
        finally:
            self._tls.repo = ""

    def _fail(self, row, name, stage, category, message, retries, hint):
        row.update({"Processing Status": STATUS_FAILED,
                    "Reason": f"{category}: {self.redact(message)}"[:500]})
        self.add_error(name, stage, category, message, retries, "Failed", hint)
        self.log_message(f"ERROR: {name} [{stage}] {category}: {self.redact(message)}")

    # ---------------- analytics ----------------
    @staticmethod
    def _ranked(series):
        counts = Counter(v for v in series if v)
        return [v for v, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    @staticmethod
    def span_days(first, last):
        return (last.date() - first.date()).days + 1

    def developer_frame(self, df):
        if df.empty:
            return pd.DataFrame(columns=DEV_SUMMARY_COLS)
        total, rows = len(df), []
        for key, g in df.groupby("Normalised Developer Identity", sort=False):
            dates = g["Date Used (UTC)"]
            first, last = dates.min(), dates.max()
            names, emails = self._ranked(g["Developer Name"]), self._ranked(g["Developer Email"])
            repos = sorted(g["Repository Full Name"].unique())
            active = int(dates.dt.normalize().nunique())
            rows.append({
                "Developer Name": names[0] if names else "(unknown)",
                "Developer Email": emails[0] if emails else "",
                "Normalised Developer Identity": key,
                "Identity Method": self._ranked(g["Identity Method"])[0],
                "Total Commits": len(g),
                "Share of Total Commits (%)": round(len(g) / total * 100, 2),
                "Repository Count": len(repos), "Repository Names": ", ".join(repos),
                "First Commit Date (UTC)": first.to_pydatetime(),
                "Latest Commit Date (UTC)": last.to_pydatetime(),
                "Commit Activity Duration (Days)": self.span_days(first, last),
                "Active Days": active,
                "Avg Commits per Active Day": round(len(g) / active, 2),
                "Name Variants": "; ".join(names), "Email Variants": "; ".join(emails)})
        out = pd.DataFrame(rows).sort_values(["Total Commits", "Developer Name"],
                                             ascending=[False, True]).reset_index(drop=True)
        out.insert(0, "Rank", range(1, len(out) + 1))
        return out[DEV_SUMMARY_COLS]

    def repository_frame(self, df):
        groups = dict(tuple(df.groupby("Repository Full Name"))) if not df.empty else {}
        total, rows = len(df), []
        for row in self.repo_rows:
            if row["Processing Status"] != STATUS_ANALYSED:
                continue
            g = groups.get(row["Repository Full Name"])
            rec = {"Repository Name": row["Repository Name"],
                   "Repository Full Name": row["Repository Full Name"]}
            if g is None:
                rec.update({"Total Commits": 0, "Unique Developers": 0,
                            "First Commit Date (UTC)": None, "Latest Commit Date (UTC)": None,
                            "Commit Activity Duration (Days)": 0, "Active Days": 0,
                            "Share of Total Commits (%)": 0.0})
            else:
                d = g["Date Used (UTC)"]
                rec.update({
                    "Total Commits": len(g),
                    "Unique Developers": int(g["Normalised Developer Identity"].nunique()),
                    "First Commit Date (UTC)": d.min().to_pydatetime(),
                    "Latest Commit Date (UTC)": d.max().to_pydatetime(),
                    "Commit Activity Duration (Days)": self.span_days(d.min(), d.max()),
                    "Active Days": int(d.dt.normalize().nunique()),
                    "Share of Total Commits (%)": round(len(g) / total * 100, 2)})
            rows.append(rec)
        out = pd.DataFrame(rows, columns=REPO_SUMMARY_COLS)
        return out.sort_values(["Total Commits", "Repository Full Name"],
                               ascending=[False, True]).reset_index(drop=True)

    def counts(self):
        c = {s: 0 for s in ALL_STATUSES}
        for row in self.repo_rows:
            c[row["Processing Status"]] += 1
        discovered = len(self.repo_rows)
        parts = sum(c.values())
        names = [r["Repository Full Name"] for r in self.repo_rows]
        return {
            "discovered": discovered, "selected": discovered - c[STATUS_EXCLUDED],
            "analysed": c[STATUS_ANALYSED], "excluded": c[STATUS_EXCLUDED],
            "skipped": c[STATUS_SKIPPED], "failed": c[STATUS_FAILED],
            "cancelled": c[STATUS_CANCELLED],
            "reconciled": parts == discovered and len(set(names)) == len(names)}

    def metrics(self, df, dev_df, repo_df):
        if df.empty:
            first = last = None
            span = active = 0
        else:
            d = df["Date Used (UTC)"]
            first, last = d.min().to_pydatetime(), d.max().to_pydatetime()
            span, active = self.span_days(d.min(), d.max()), int(d.dt.normalize().nunique())
        return {
            "total_commits": int(len(df)), "unique_devs": int(len(dev_df)),
            "pairs": int(dev_df["Repository Count"].sum()) if len(dev_df) else 0,
            "first": first, "last": last, "span": span, "active": active,
            "inactive": span - active,
            "repos_with_commits": int((repo_df["Total Commits"] > 0).sum())
            if len(repo_df) else 0}

    def integrity_checks(self, df, dev_df, repo_df, mt, ct):
        checks = []

        def add(name, expected, actual, detail="", warn=False):
            ok = expected == actual
            checks.append({"Check": name, "Expected": str(expected), "Actual": str(actual),
                           "Result": "PASS" if ok else ("WARN" if warn else "FAIL"),
                           "Detail": detail})
        total = mt["total_commits"]
        add("Sum of developer commit counts = total commits", total,
            int(dev_df["Total Commits"].sum()) if len(dev_df) else 0)
        add("Sum of repository commit counts = total commits", total,
            int(repo_df["Total Commits"].sum()) if len(repo_df) else 0)
        add("Commit Details rows = total commits", total, len(df))
        add("Repository Status 'Commits Found' = total commits", total,
            sum(r["Commits Found"] for r in self.repo_rows))
        add("Active Developers rows = unique developers",
            int(df["Normalised Developer Identity"].nunique()) if len(df) else 0,
            mt["unique_devs"])
        add("Repository reconciliation: discovered = analysed + excluded + skipped + "
            "failed + cancelled", ct["discovered"],
            ct["analysed"] + ct["excluded"] + ct["skipped"] + ct["failed"] + ct["cancelled"])
        add("Every discovered repository has exactly one status row", ct["discovered"],
            len({r["Repository Full Name"] for r in self.repo_rows}))
        add("Repository Summary rows = analysed repositories", ct["analysed"], len(repo_df))
        parts = sum(r["Excluded: Bots"] + r["Excluded: Merges"] + r["Excluded: Outside Period"]
                    + r["Excluded: Invalid/Duplicate Records"] + r["Commits Found"]
                    for r in self.repo_rows)
        add("Commit ledger: commits read = qualifying + every excluded bucket",
            sum(r["Commits Read (all history)"] for r in self.repo_rows), parts)
        add("Active days + inactive days = activity span", mt["span"],
            mt["active"] + mt["inactive"])
        if len(df):
            inside = int(((df["Date Used (UTC)"] >= self.start_date.replace(tzinfo=None))
                          & (df["Date Used (UTC)"] <= self.end_date.replace(tzinfo=None))).sum())
            add("All commit dates fall inside the reporting period", total, inside)
            dup_pairs = int(df.duplicated(["Repository Full Name", "Commit Hash"]).sum())
            add("No duplicate commit within a repository", 0, dup_pairs)
            shared = df.groupby("Commit Hash")["Repository Full Name"].nunique()
            n_shared = int((shared > 1).sum())
            add("Commit hashes shared by several repositories (forks/mirrors)", 0, n_shared,
                "Shared history is counted once per repository, so total commits may "
                "include the same commit in more than one repository." if n_shared else "",
                warn=True)
        for warning in self.warnings:
            checks.append({"Check": "Discovery / run warning", "Expected": "none",
                           "Actual": "warning", "Result": "WARN", "Detail": warning})
        return checks

    def outcome(self, ct, cancelled, failures):
        if cancelled or ct["cancelled"]:
            return "Cancelled analysis (partial report)"
        if not self.discovery_complete:
            return "Incomplete repository discovery (partial report)"
        if ct["failed"]:
            return "Partially successful analysis (some repositories failed)"
        if failures:
            return "Completed with integrity-check failures"
        if ct["analysed"] == 0:
            return "No repositories were analysed"
        if ct["skipped"]:
            return "Analysis with skipped repositories"
        return "Fully successful analysis"

    # ---------------- report ----------------
    def _report_path(self, suffix):
        owner = re.sub(r"[^A-Za-z0-9_.-]", "_", getattr(self, "source_owner", "") or "report")
        stem = f"Git_Developer_Activity_{owner}_{self.days}d_{self.date_basis}_{self.run_id}{suffix}"
        path, counter = self.output_dir / f"{stem}.xlsx", 2
        while path.exists():                       # never overwrite an existing report
            path = self.output_dir / f"{stem}_{counter}.xlsx"
            counter += 1
        return path

    def generate_report(self, input_url, cancelled=False):
        df = pd.DataFrame(self.commit_records, columns=COMMIT_COLS)
        if not df.empty:
            df["Date Used (UTC)"] = pd.to_datetime(df["Date Used (UTC)"])
            df = df.sort_values(["Date Used (UTC)", "Repository Full Name", "Commit Hash"],
                                ascending=[False, True, True]).reset_index(drop=True)
        dev_df, repo_df = self.developer_frame(df), self.repository_frame(df)
        ct, mt = self.counts(), self.metrics(df, dev_df, repo_df)
        checks = self.integrity_checks(df, dev_df, repo_df, mt, ct)
        failures = sum(1 for c in checks if c["Result"] == "FAIL")
        label = self.outcome(ct, cancelled, failures)
        self.stats = {
            **ct, "commits": mt["total_commits"], "developers": mt["unique_devs"],
            "pairs": mt["pairs"], "span": mt["span"], "active_days": mt["active"],
            "inactive_days": mt["inactive"], "first": mt["first"], "last": mt["last"],
            "date_basis": self.date_basis, "integrity_failures": failures,
            "outcome": label}
        ctx = {"url": input_url, "df": df, "dev": dev_df, "repo": repo_df, "ct": ct,
               "mt": mt, "checks": checks, "failures": failures, "label": label}

        suffix = "" if label.startswith("Fully") else (
            "_CANCELLED" if label.startswith("Cancelled") else "_PARTIAL")
        self.log_message("Writing Excel report...")
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            final = self._report_path(suffix)
            tmp = final.with_name(final.name + ".tmp")
            try:
                build_workbook(self, ctx).save(tmp)
                while final.exists():
                    final = self._report_path(suffix)
                os.replace(tmp, final)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
        except OSError as exc:
            fallback = ""
            try:
                fb = self.output_dir / f"commit_details_fallback_{self.run_id}.csv"
                df.to_csv(fb, index=False, encoding="utf-8-sig")
                fallback = f" A CSV copy of the commit data was saved to {fb}."
            except Exception:
                pass
            if exc.errno == errno.ENOSPC:
                cat, hint = "Disk space", "Free disk space and re-run."
            elif isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
                cat, hint = "Excel/filesystem permission", (
                    "Close the report if open in Excel and check folder permissions.")
            else:
                cat, hint = "Filesystem", "Check the output folder path and filename."
            raise AppError(f"Cannot write the Excel report: {self.redact(exc)}.{fallback}",
                           cat, hint, stage="Excel report")
        problems = validate_workbook(final)
        self.stats["validation_problems"] = problems
        for p in problems:
            self.log_message(f"WORKBOOK VALIDATION PROBLEM: {p}")
        self.stats["report"] = str(final)
        return final

    # ---------------- main flow ----------------
    def analyse(self, input_url):
        stage_git = self.run_git(["--version"], stage="Git check")
        m = re.search(r"(\d+)\.(\d+)", stage_git)
        self.git_version = (int(m.group(1)), int(m.group(2))) if m else None
        self.log_message(f"Git: {describe_git_runtime()} - {stage_git.strip()}")
        if self.token and self.git_version and self.git_version < (2, 31):
            raise AppError(f"Git {stage_git.strip()} is too old for secure token "
                           "authentication.", "Git version",
                           "Install Git 2.31 or newer (the packaged application ships a "
                           "current PortableGit).", stage="Git check")
        self.cleanup_stale_temp()
        parsed = self.parse_github_url(input_url)
        owner = parsed["owner"]
        self.source_owner = owner
        self.source_url = f"https://github.com/{owner}" + (
            f"/{parsed['repository']}" if parsed["repository"] else "")
        self.log_message("=" * 65)
        self.log_message(f"Reporting period: last {self.days} days (date basis: "
                         f"{self.date_basis} date)")
        self.log_message(f"Start (UTC): {self.start_date:%Y-%m-%d %H:%M:%S}")
        self.log_message(f"End   (UTC): {self.end_date:%Y-%m-%d %H:%M:%S}")
        self.log_message("=" * 65)

        invalid = []
        self.is_single_repo = parsed["type"] == "repository"
        if self.is_single_repo:
            self.log_message(f"Looking up repository: {owner}/{parsed['repository']}")
            repo = self.normalise_repo(self.api_get(f"/repos/{owner}/{parsed['repository']}"))
            if repo is None:
                raise AppError("GitHub returned an unusable repository record.",
                               "Malformed response", "Retry later.", stage="Repository lookup")
            repos = [repo]
            self.log_message("Explicit repository URL: fork/archived/disabled filters "
                             "are not applied.")
        else:
            repos, invalid = self.discover_repositories(owner)

        for repo in sorted(repos, key=lambda r: r["full_name"].lower()):
            row = self.new_row(repo)
            reason = "" if self.is_single_repo else self.exclusion_reason(repo)
            if reason:
                row.update({"Processing Status": STATUS_EXCLUDED, "Reason": reason})
            self.repo_rows.append(row)
        for label in invalid:
            stub = {"name": label, "full_name": label, "html_url": "", "visibility": "unknown",
                    "fork": False, "archived": False, "disabled": False, "pushed_at": None}
            row = self.new_row(stub)
            row.update({"Processing Status": STATUS_FAILED,
                        "Reason": "Unreadable API record (missing/invalid name or owner)."})
            self.repo_rows.append(row)
            self.add_error(label, "Repository discovery", "Malformed response",
                           "API record lacked a valid name/owner.", 0, "Failed",
                           "Re-run; report to GitHub support if persistent.")

        queue_rows = [r for r in self.repo_rows if r["Processing Status"] == STATUS_CANCELLED]
        total = len(queue_rows)
        self.log_message(f"Repositories discovered: {len(self.repo_rows)}; "
                         f"selected for analysis: {total + sum(1 for r in self.repo_rows if r['Processing Status'] == STATUS_FAILED)}")
        self.progress(0, total)
        cancelled = False
        if total:
            workers = max(1, min(MAX_PARALLEL_REPOS, total))
            self._parallel = workers > 1
            if self._parallel:
                self.log_message(f"Processing up to {workers} repositories in parallel.")
            done = 0
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="repo") as pool:
                futures = [pool.submit(self._run_repo_task, index, total, row)
                           for index, row in enumerate(queue_rows, 1)]
                for future in as_completed(futures):
                    try:
                        cancelled = bool(future.result()) or cancelled
                    except Exception:                       # noqa: BLE001
                        file_logger.error(self.redact(traceback.format_exc()))
                    done += 1
                    self.progress(done, total)
        for row in self.repo_rows:
            if row["Processing Status"] == STATUS_CANCELLED:
                row["Reason"] = "Not started - analysis was cancelled."

        report = self.generate_report(input_url, cancelled)
        s = self.stats
        self.log_message("=" * 65)
        self.log_message(s["outcome"].upper())
        self.log_message(f"Repositories : {s['discovered']} discovered = {s['analysed']} "
                         f"analysed + {s['excluded']} excluded + {s['skipped']} skipped + "
                         f"{s['failed']} failed + {s['cancelled']} cancelled/not processed")
        self.log_message(f"Developers   : {s['developers']} unique active; commits: {s['commits']}")
        self.log_message(f"Activity     : span {s['span']} day(s), commits on "
                         f"{s['active_days']}, none on {s['inactive_days']}")
        self.log_message(f"Excel report : {report}")
        self.log_message("=" * 65)
        failed = [(r["Repository Full Name"], r["Reason"]) for r in self.repo_rows
                  if r["Processing Status"] == STATUS_FAILED]
        return {"report": report, "outcome": s["outcome"], "stats": s, "failed": failed,
                "warnings": list(self.warnings), "cancelled": cancelled}


# ============================================================
# 4. EXCEL REPORT (formatting + validation)
# ============================================================
from openpyxl import Workbook, load_workbook                                  # noqa: E402
from openpyxl.formatting.rule import CellIsRule, DataBarRule, FormulaRule   # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side      # noqa: E402
from openpyxl.utils import get_column_letter                                  # noqa: E402
from openpyxl.worksheet.properties import PageSetupProperties                # noqa: E402

NAVY, MID, LIGHT, BAND, KPI = "1F4E78", "2E75B6", "DDEBF7", "F3F7FB", "EAF1FB"
GREEN, AMBER, RED = "C6EFCE", "FFEB9C", "FFC7CE"
_F = lambda **k: Font(name=FONT_NAME, size=k.pop("size", 10), **k)            # noqa: E731
_FILL = lambda c: PatternFill(start_color=c, end_color=c, fill_type="solid")  # noqa: E731
_SIDE = Side(style="thin", color="BFBFBF")
BORDER = Border(left=_SIDE, right=_SIDE, top=_SIDE, bottom=_SIDE)
A_LEFT = Alignment(horizontal="left", vertical="center")
A_LEFTW = Alignment(horizontal="left", vertical="center", wrap_text=True)
A_CENTER = Alignment(horizontal="center", vertical="center")
A_CENTERW = Alignment(horizontal="center", vertical="center", wrap_text=True)
FONT_BASE = _F()                              # shared style objects (created once)
FONT_HEAD = _F(bold=True, color="FFFFFF")
FILL_NAVY = _FILL(NAVY)
LEFT_HEADERS = {
    "Developer Name", "Developer Email", "Repository Name", "Repository Full Name",
    "Repository Names", "Repository URL", "Committer Name", "Committer Email",
    "Commit Message", "Commit Hash", "Normalised Developer Identity", "Identity Method",
    "Reason", "Error Message", "Recommended Corrective Action", "Repository",
    "Check", "Detail", "Name Variants", "Email Variants", "Processing Stage",
    "Error Category", "Expected", "Actual"}
WRAP_HEADERS = {"Commit Message": 60, "Reason": 55, "Error Message": 60,
                "Recommended Corrective Action": 45, "Detail": 60, "Repository Names": 55,
                "Name Variants": 35, "Email Variants": 40, "Check": 55, "Expected": 18,
                "Actual": 18, "Repository URL": 45}


def py(v):
    """Convert pandas/numpy values to plain, Excel-safe Python values (fast paths first)."""
    t = type(v)
    if t is str:
        return clean_text(v)
    if t is int or t is bool or t is datetime:
        return v
    if t is float:
        return None if v != v else v
    if v is None:
        return None
    if not isinstance(v, (str, bytes, list, tuple, dict)) and pd.isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        v = v.item()
    return clean_text(v) if isinstance(v, str) else v


def set_cell(cell, value):
    cell.value = value
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"          # commit text must never become a formula


def write_table(ws, columns, rows, freeze_cols=0, status_col=None, bar_col=None,
                result_col=None):
    """Write a header + data rows with the standard professional formatting.
    Returns the last data row.  Cell styles are built once per (column, value-kind) and
    then copied onto every cell, which is far faster than styling each cell separately."""
    ncols = len(columns)
    for c, name in enumerate(columns, 1):
        cell = ws.cell(1, c, name)
        cell.font, cell.fill = FONT_HEAD, FILL_NAVY
        cell.alignment, cell.border = A_CENTERW, BORDER
    ws.row_dimensions[1].height = 34
    meta = []
    for name in columns:
        left, wrap = name in LEFT_HEADERS, name in WRAP_HEADERS
        meta.append((wrap, WRAP_HEADERS.get(name, 45),
                     (A_LEFTW if wrap else A_LEFT) if left else A_CENTER))
    number_formats = {1: DATE_FMT, 3: "#,##0", 4: "0.00"}
    templates = {}
    widths = [min(len(n) + 3, 18) for n in columns]
    last = 1
    for r, values in enumerate(rows, 2):
        last, lines = r, 1
        for c, raw in enumerate(values, 1):
            if c > ncols:
                break
            v = py(raw)
            cell = ws.cell(r, c)
            set_cell(cell, v)
            wrap, cap, alignment = meta[c - 1]
            size = 0
            if isinstance(v, datetime):
                kind, size = 1, 20
            elif isinstance(v, bool):
                kind, size = 2, 5
            elif isinstance(v, int):
                kind = 3
                if r <= 400:
                    size = len(str(v)) + 3
            elif isinstance(v, float):
                kind = 4
                if r <= 400:
                    size = len(f"{v:.2f}") + 3
            else:
                kind = 0
                if v is not None and (wrap or r <= 400):
                    text = str(v)
                    size = min(len(text) + 3, cap)
                    if wrap and text:
                        lines = max(lines, -(-len(text) // max(cap - 2, 1)))
            template = templates.get((c, kind))
            if template is None:
                cell.border, cell.font, cell.alignment = BORDER, FONT_BASE, alignment
                if kind in number_formats:
                    cell.number_format = number_formats[kind]
                templates[(c, kind)] = copy(cell._style)
            else:
                cell._style = copy(template)
            if r <= 400 and size > widths[c - 1]:
                widths[c - 1] = size
        if lines > 1:
            ws.row_dimensions[r].height = min(13.5 * lines, 200)
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = max(10, min(w, 70))
    ws.freeze_panes = ws.cell(2, freeze_cols + 1).coordinate
    letter = get_column_letter(ncols)
    ws.auto_filter.ref = f"A1:{letter}{max(last, 2)}"
    if last >= 2:
        rng = lambda col: f"{get_column_letter(col)}2:{get_column_letter(col)}{last}"  # noqa: E731
        if status_col:
            for text, color in ((STATUS_ANALYSED, GREEN), (STATUS_FAILED, RED),
                                (STATUS_EXCLUDED, "E7E6E6"), (STATUS_SKIPPED, AMBER),
                                (STATUS_CANCELLED, "F8CBAD")):
                ws.conditional_formatting.add(rng(status_col), CellIsRule(
                    operator="equal", formula=[f'"{text}"'], fill=_FILL(color)))
        if result_col:
            for text, color in (("PASS", GREEN), ("WARN", AMBER), ("FAIL", RED)):
                ws.conditional_formatting.add(rng(result_col), CellIsRule(
                    operator="equal", formula=[f'"{text}"'], fill=_FILL(color)))
        if bar_col:
            ws.conditional_formatting.add(rng(bar_col), DataBarRule(
                start_type="num", start_value=0, end_type="max", color="9DC3E6"))
        ws.conditional_formatting.add(f"A2:{letter}{last}", FormulaRule(
            formula=["MOD(ROW(),2)=0"], fill=_FILL(BAND)))
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_title_rows = "1:1"
    return last


def write_totals(ws, columns, last_row, values, notes):
    """TOTAL row 3 rows below the data (outside the filter range) + notes."""
    r = last_row + 4
    for c, name in enumerate(columns, 1):
        cell = ws.cell(r, c)
        cell.fill, cell.font = _FILL(LIGHT), _F(bold=True)
        cell.border = Border(top=Side(style="medium", color=NAVY), bottom=_SIDE)
        cell.alignment = A_LEFT if name in LEFT_HEADERS else A_CENTER
        if c == 1:
            cell.value = "TOTAL"
        if name in values:
            v = py(values[name])
            set_cell(cell, v)
            if isinstance(v, datetime):
                cell.number_format = DATE_FMT
            elif isinstance(v, float):
                cell.number_format = "0.00"
            elif isinstance(v, int):
                cell.number_format = "#,##0"
    for i, note in enumerate(notes, 1):
        cell = ws.cell(r + i, 1, note)
        cell.font, cell.alignment = _F(italic=True, color="555555", size=9), A_LEFT


def df_rows(df, columns):
    return df[columns].itertuples(index=False, name=None) if len(df) else []


def build_workbook(an, ctx):
    df, dev, repo, ct, mt = ctx["df"], ctx["dev"], ctx["repo"], ctx["ct"], ctx["mt"]
    wb = Workbook()
    wb.properties.title = f"{APP_TITLE} v{APP_VERSION}"
    wb.properties.creator = f"{APP_TITLE} v{APP_VERSION}"
    wb.properties.subject = "GitHub developer activity report"
    wb.remove(wb.active)
    summary = wb.create_sheet("Summary")
    ws_act = wb.create_sheet("Active Developers")
    ws_dev = wb.create_sheet("Developer Summary")
    ws_repo = wb.create_sheet("Repository Summary")
    ws_com = wb.create_sheet("Commit Details")
    ws_stat = wb.create_sheet("Repository Status")
    ws_err = wb.create_sheet("Processing Errors")
    ws_chk = wb.create_sheet("Integrity Checks")
    for ws, color in ((summary, NAVY), (ws_act, MID), (ws_dev, MID), (ws_repo, MID),
                      (ws_com, "7F7F7F"), (ws_stat, "548235"), (ws_err, "C00000"),
                      (ws_chk, "BF8F00")):
        ws.sheet_properties.tabColor = color

    span_note = ("Overall activity span and active days are computed across all rows "
                 "together; they are NOT sums of the rows above.")
    # --- Active Developers
    last = write_table(ws_act, ACTIVE_DEV_COLS, df_rows(dev, ACTIVE_DEV_COLS),
                       freeze_cols=1, bar_col=3)
    write_totals(ws_act, ACTIVE_DEV_COLS, last, {
        "Developer Name": f"{mt['unique_devs']} unique active developers",
        "Total Commits": mt["total_commits"], "Repository Count": mt["pairs"],
        "First Commit Date (UTC)": mt["first"], "Latest Commit Date (UTC)": mt["last"],
        "Commit Activity Duration (Days)": mt["span"], "Active Days": mt["active"],
        "Share of Total Commits (%)": 100.0 if mt["total_commits"] else 0.0},
        [f"Repository Count total = {mt['pairs']} developer-repository pairs (a developer in "
         f"3 repositories counts 3 times); unique developers = {mt['unique_devs']}.", span_note])
    # --- Developer Summary
    last = write_table(ws_dev, DEV_SUMMARY_COLS, df_rows(dev, DEV_SUMMARY_COLS),
                       freeze_cols=2, bar_col=6)
    write_totals(ws_dev, DEV_SUMMARY_COLS, last, {
        "Developer Name": f"{mt['unique_devs']} unique developers",
        "Total Commits": mt["total_commits"], "Repository Count": mt["pairs"],
        "Share of Total Commits (%)": 100.0 if mt["total_commits"] else 0.0,
        "First Commit Date (UTC)": mt["first"], "Latest Commit Date (UTC)": mt["last"],
        "Commit Activity Duration (Days)": mt["span"], "Active Days": mt["active"]},
        [f"Repository Count total = {mt['pairs']} developer-repository pairs; unique "
         f"developers = {mt['unique_devs']}.", span_note,
         "Identity = GitHub noreply login, else e-mail, else display name. Different e-mails "
         "of one person appear as separate rows unless merged via identity_aliases.json."])
    # --- Repository Summary
    last = write_table(ws_repo, REPO_SUMMARY_COLS, df_rows(repo, REPO_SUMMARY_COLS),
                       freeze_cols=2, bar_col=3)
    write_totals(ws_repo, REPO_SUMMARY_COLS, last, {
        "Repository Full Name": f"{len(repo)} analysed repositories",
        "Total Commits": mt["total_commits"], "Unique Developers": mt["pairs"],
        "First Commit Date (UTC)": mt["first"], "Latest Commit Date (UTC)": mt["last"],
        "Commit Activity Duration (Days)": mt["span"], "Active Days": mt["active"],
        "Share of Total Commits (%)": 100.0 if mt["total_commits"] else 0.0},
        [f"'Unique Developers' total = {mt['pairs']} (a person active in 3 repositories is "
         f"counted 3 times); distinct people overall = {mt['unique_devs']}.", span_note])
    # --- Commit Details (split only if Excel's row limit would be exceeded)
    chunks = [df.iloc[i:i + MAX_EXCEL_DATA_ROWS] for i in range(0, max(len(df), 1),
                                                                MAX_EXCEL_DATA_ROWS)]
    for n, chunk in enumerate(chunks):
        ws = ws_com if n == 0 else wb.create_sheet(f"Commit Details ({n + 1})")
        if n:
            ws.sheet_properties.tabColor = "7F7F7F"
        last = write_table(ws, COMMIT_COLS, df_rows(chunk, COMMIT_COLS), freeze_cols=2)
        if n == len(chunks) - 1:
            write_totals(ws, COMMIT_COLS, last, {
                "Repository Name": f"{mt['repos_with_commits']} repositories with commits",
                "Developer Name": f"{mt['unique_devs']} unique developers",
                "Commit Hash": f"{mt['total_commits']:,} commits in total"},
                ["Totals cover all Commit Details sheets. All timestamps are UTC; "
                 f"'Date Used (UTC)' is the {an.date_basis} date used for filtering."])
    # --- Repository Status
    status_rows = [[row[c] for c in STATUS_COLS] for row in an.repo_rows]
    last = write_table(ws_stat, STATUS_COLS, status_rows, freeze_cols=2,
                       status_col=STATUS_COLS.index("Processing Status") + 1)
    write_totals(ws_stat, STATUS_COLS, last, {
        "Repository Full Name": f"{ct['discovered']} discovered",
        "Commits Read (all history)": sum(r["Commits Read (all history)"] for r in an.repo_rows),
        "Commits Found": mt["total_commits"],
        "Excluded: Bots": sum(r["Excluded: Bots"] for r in an.repo_rows),
        "Excluded: Merges": sum(r["Excluded: Merges"] for r in an.repo_rows),
        "Excluded: Outside Period": sum(r["Excluded: Outside Period"] for r in an.repo_rows),
        "Excluded: Invalid/Duplicate Records": sum(
            r["Excluded: Invalid/Duplicate Records"] for r in an.repo_rows),
        "Processing Status": f"{ct['analysed']} ok / {ct['excluded']} excl / "
                             f"{ct['skipped']} skip / {ct['failed']} fail / "
                             f"{ct['cancelled']} cancel"},
        [f"Reconciliation: {ct['discovered']} discovered = {ct['analysed']} analysed + "
         f"{ct['excluded']} excluded + {ct['skipped']} skipped + {ct['failed']} failed + "
         f"{ct['cancelled']} cancelled/not processed.",
         "Commit ledger: Commits Read = Commits Found + all 'Excluded:' columns."])
    # --- Processing Errors
    err_rows = [[e[c] for c in ERROR_COLS] for e in an.processing_errors] or [[
        datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0), "-", "-", "None",
        "No processing errors occurred.", 0, "N/A", "None required."]]
    write_table(ws_err, ERROR_COLS, err_rows, freeze_cols=2)
    ws_err.conditional_formatting.add("G2:G%d" % (len(err_rows) + 1), CellIsRule(
        operator="equal", formula=['"Failed"'], fill=_FILL(RED)))
    # --- Integrity Checks
    write_table(ws_chk, INTEGRITY_COLS, [[c[k] for k in INTEGRITY_COLS] for c in ctx["checks"]],
                result_col=4)
    build_summary(an, summary, ctx)
    wb.active = 0
    for sheet in wb.worksheets:
        sheet.sheet_view.tabSelected = sheet is summary
    return wb


def build_summary(an, ws, ctx):
    ct, mt, dev, label = ctx["ct"], ctx["mt"], ctx["dev"], ctx["label"]
    ws.sheet_view.showGridLines = False
    for col, w in (("A", 44), ("B", 38), ("C", 95)):
        ws.column_dimensions[col].width = w
    for r, (text, font, fill) in enumerate((
            ("GitHub Developer Activity Analytics - Summary Report",
             _F(size=18, bold=True, color="FFFFFF"), NAVY),
            (f"Source: {ctx['url']}", _F(size=10, color="FFFFFF"), MID),
            (f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC | Point-in-time "
             "snapshot of the Git history available at that moment (not continuous "
             "monitoring) | All timestamps are UTC", _F(size=9, italic=True, color="FFFFFF"),
             MID)), 1):
        for c in range(1, 4):
            ws.cell(r, c).fill = _FILL(fill)
        cell = ws.cell(r, 1, text)
        cell.font, cell.alignment = font, A_LEFT
    ws.row_dimensions[1].height = 32
    ok = label.startswith("Fully")
    bad = label.startswith(("Cancelled", "Incomplete", "Partially"))
    ws.cell(5, 1, "RUN OUTCOME").font = _F(bold=True, size=11)
    ws.cell(5, 1).alignment = A_LEFT
    ws.merge_cells("B5:C5")
    out = ws.cell(5, 2, label)
    out.font, out.alignment = _F(bold=True, size=12), A_LEFT
    for c in (2, 3):
        ws.cell(5, c).fill = _FILL(GREEN if ok else (RED if bad else AMBER))
    ws.row_dimensions[5].height = 24

    yes = lambda f, a, b: a if f else b                      # noqa: E731
    rows_by = lambda key, sub: sum(                          # noqa: E731
        1 for r in an.repo_rows if r["Processing Status"] == STATUS_EXCLUDED and sub in r["Reason"])
    led = lambda k: sum(r[k] for r in an.repo_rows)          # noqa: E731
    recon = (f"{'RECONCILED' if ct['reconciled'] else 'NOT RECONCILED'}: {ct['discovered']} "
             f"discovered = {ct['analysed']} analysed + {ct['excluded']} excluded + "
             f"{ct['skipped']} skipped + {ct['failed']} failed + {ct['cancelled']} "
             "cancelled/not processed")
    basis = ("Author date (when the developer wrote the commit)" if an.date_basis == "author"
             else "Committer date (when the commit was applied/merged/rebased)")
    acct = (an.account or {}).get("type") or "Repository (direct URL)"
    N, T, D = "0", "@", DATE_FMT
    sections = [
        ("HEADLINE KPIs", [
            ("Unique active developers", mt["unique_devs"],
             "People with at least one qualifying commit in the reporting period.", N),
            ("Total commits", mt["total_commits"], "Qualifying commits, each counted once "
             "per repository.", "#,##0"),
            ("Repositories analysed", f"{ct['analysed']} of {ct['discovered']} discovered",
             "Fully read without errors.", T),
            ("Days with commits", f"{mt['active']} of {mt['span']} days in span",
             "Calendar days (UTC) on which anyone committed.", T)], True),
        ("1. REPORT DETAILS", [
            ("Report title", "GitHub Developer Activity Analytics", f"Version {APP_VERSION}.", T),
            ("GitHub source URL", ctx["url"], "The link that was analysed.", T),
            ("Account type", acct, "Detected through the GitHub API.", T),
            ("Generated (UTC)", datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0),
             "Time the report was produced.", D)], False),
        ("2. REPORTING PERIOD", [
            ("Reporting period (days)", an.days, "Configured window length.", N),
            ("Period start (UTC)", an.start_date.replace(tzinfo=None), "Inclusive.", D),
            ("Period end (UTC)", an.end_date.replace(tzinfo=None),
             "Inclusive; moment the analysis started.", D),
            ("Time zone", "UTC", "All internal calculations and displayed times.", T),
            ("Date basis", basis, "Used for filtering, developers, repositories and "
             "activity metrics. Both dates are stored in Commit Details.", T)], False),
        ("3. REPOSITORY RECONCILIATION", [
            ("Repositories discovered", ct["discovered"], "Every repository returned by the "
             "GitHub API for this scope (no duplicates).", N),
            ("Repositories selected", ct["selected"], "Discovered minus excluded by filters.", N),
            ("Successfully analysed", ct["analysed"], "", N),
            ("Excluded", ct["excluded"], "Removed by the fork/archived/disabled filters "
             "(reason shown per repository).", N),
            ("Skipped", ct["skipped"], "Empty repositories, or no push in the period when "
             "'skip inactive' is on.", N),
            ("Failed", ct["failed"], "See Processing Errors.", N),
            ("Cancelled / not processed", ct["cancelled"], "Not reached because the run was "
             "cancelled.", N),
            ("Reconciliation result", recon, "Must hold for every run.", T),
            ("Repository discovery complete", yes(an.discovery_complete, "Yes",
             "NO - interrupted"), "Pagination finished without interruption.", T)], False),
        ("4. COMMIT & DEVELOPER RESULTS", [
            ("Total commits", mt["total_commits"], "Equals Commit Details rows.", "#,##0"),
            ("Unique active developers", mt["unique_devs"], "Equals Active Developers rows.", N),
            ("Developer-repository pairs", mt["pairs"], "A developer in 3 repositories counts "
             "3 times here, once in unique developers.", N),
            ("Repositories with commits", mt["repos_with_commits"], "", N),
            ("First commit (UTC)", mt["first"] or "No commits", "Earliest qualifying commit.", D),
            ("Latest commit (UTC)", mt["last"] or "No commits", "Latest qualifying commit.", D),
            ("Commit activity span (days)", mt["span"], "Inclusive calendar days from first "
             "to latest commit. This is a period length, not days worked.", N),
            ("Active days", mt["active"], "Distinct calendar dates with at least one commit "
             "across all repositories.", N),
            ("Inactive days within span", mt["inactive"], "Span minus active days.", N)], False),
        ("5. FILTERS APPLIED", [
            ("Forks", yes(an.include_forks, "Included", "Excluded"),
             f"{rows_by('f', 'Fork')} repositories excluded by this filter." if not an.include_forks
             else "Forks carry upstream authors and can inflate developer counts.", T),
            ("Archived repositories", yes(an.include_archived, "Included", "Excluded"),
             f"{rows_by('a', 'Archived')} repositories excluded by this filter." if not
             an.include_archived else "Read-only repositories included.", T),
            ("Disabled repositories", yes(an.include_disabled, "Included", "Excluded"),
             f"{rows_by('d', 'Disabled')} repositories excluded by this filter." if not
             an.include_disabled else "Included; they normally fail to clone.", T),
            ("Bot accounts", yes(an.exclude_bots, "Excluded", "Included"),
             f"{led('Excluded: Bots')} commits excluded." if an.exclude_bots else
             "Bot commits are counted as developers.", T),
            ("Merge commits", yes(an.exclude_merges, "Excluded", "Included"),
             f"{led('Excluded: Merges')} commits excluded." if an.exclude_merges else
             "Merge commits are counted.", T),
            ("Inactive repositories", yes(an.skip_inactive, "Skipped", "Processed"),
             "Skipping uses the last push date; processing everything is the most exact.", T),
            ("Commits outside the period", led("Excluded: Outside Period"),
             "Commits read from history but dated outside the reporting window.", N)], False),
        ("6. RUN NOTES & LIMITATIONS", [
            ("Warnings", f"{len(an.warnings)} warning(s)" if an.warnings else "None",
             " | ".join(an.warnings) or "No discovery or run warnings.", T),
            ("Integrity checks", f"{sum(1 for c in ctx['checks'] if c['Result'] == 'PASS')} pass / "
             f"{sum(1 for c in ctx['checks'] if c['Result'] == 'WARN')} warn / "
             f"{ctx['failures']} fail", "Details on the Integrity Checks sheet.", T),
            ("History scope", "Reachable refs only", "Commits come from branches/tags fetched "
             "by Git. Deleted, force-pushed-away or unreachable history cannot be retrieved.", T),
            ("Developer identity", "Commit metadata", "Identity = GitHub noreply login, else "
             "e-mail, else name. Display names are never used to merge people who have "
             "e-mails; one person using several e-mails appears several times unless mapped in "
             "identity_aliases.json. Commit author e-mails are self-declared and can be wrong.",
             T),
            ("Commit counts", "Per repository", "Forks/mirrors share history; the same commit in "
             "two repositories is counted in each (see Integrity Checks).", T)], False)]

    r = 7
    for title, rows, kpi in sections:
        for c in range(1, 4):
            ws.cell(r, c).fill = _FILL(MID)
        ws.cell(r, 1, title).font = _F(bold=True, color="FFFFFF", size=11)
        ws.cell(r, 1).alignment = A_LEFT
        r += 1
        for c, t in enumerate(("Metric", "Value", "What it means"), 1):
            cell = ws.cell(r, c, t)
            cell.font, cell.fill = _F(bold=True), _FILL(LIGHT)
            cell.alignment, cell.border = (A_CENTER if c == 2 else A_LEFT), BORDER
        r += 1
        for metric, value, meaning, fmt in rows:
            value = py(value)
            a, b, c = ws.cell(r, 1, metric), ws.cell(r, 2), ws.cell(r, 3, meaning)
            set_cell(b, value)
            b.number_format = fmt
            a.font, c.font = _F(bold=True), _F()
            b.font = _F(bold=True, size=13 if kpi else 10)
            b.alignment = A_CENTERW if isinstance(value, (int, float, datetime)) or kpi \
                else A_LEFTW
            a.alignment, c.alignment = A_LEFTW, A_LEFTW
            for cell in (a, b, c):
                cell.border = BORDER
                if kpi:
                    cell.fill = _FILL(KPI)
            lines = max(-(-len(str(metric)) // 42), -(-len(str(meaning)) // 92),
                        -(-len(str(value if value is not None else "")) // 36)
                        if isinstance(value, str) else 1)
            ws.row_dimensions[r].height = max(20 if kpi else 16, 13.5 * lines + 3)
            r += 1
        r += 1
    # Complete list of active developers
    for c in range(1, 4):
        ws.cell(r, c).fill = _FILL(MID)
    ws.cell(r, 1, f"7. ACTIVE DEVELOPERS - COMPLETE LIST ({mt['unique_devs']})").font = _F(
        bold=True, color="FFFFFF", size=11)
    r += 1
    for c, t in enumerate(("Developer", "Total Commits", "E-mail (most frequent)"), 1):
        cell = ws.cell(r, c, t)
        cell.font, cell.fill, cell.border = _F(bold=True), _FILL(LIGHT), BORDER
        cell.alignment = A_CENTER if c == 2 else A_LEFT
    r += 1
    for name, commits, email in (dev[["Developer Name", "Total Commits", "Developer Email"]]
                                 .itertuples(index=False, name=None) if len(dev) else []):
        for c, v in enumerate((name, commits, email), 1):
            cell = ws.cell(r, c)
            set_cell(cell, py(v))
            cell.font, cell.border = FONT_BASE, BORDER
            cell.alignment = A_CENTER if c == 2 else A_LEFT
        ws.cell(r, 2).number_format = "#,##0"
        r += 1
    if not len(dev):
        ws.cell(r, 1, "No active developers in the reporting period.").font = _F(italic=True)
        r += 1
    r += 1
    ws.cell(r, 1, "Worksheets: Active Developers | Developer Summary | Repository Summary | "
                  "Commit Details | Repository Status | Processing Errors | Integrity Checks"
            ).font = _F(italic=True, size=9, color="555555")
    ws.freeze_panes = "A6"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)


def validate_workbook(path):
    """Re-open the saved file (read-only streaming, so large reports stay cheap) and
    cross-check sheets against each other.  Returns a list of problem descriptions
    (empty = consistent)."""
    try:
        wb = load_workbook(path, read_only=True)
    except Exception as exc:                                   # noqa: BLE001
        return [f"Workbook cannot be re-opened: {exc}"]
    try:
        return _validate_open_workbook(wb)
    except Exception as exc:                                   # noqa: BLE001
        return [f"Workbook validation failed: {type(exc).__name__}: {exc}"]
    finally:
        try:
            wb.close()
        except Exception:                                      # noqa: BLE001
            pass


def _validate_open_workbook(wb):
    problems = []

    def headers(ws):
        first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        return list(first)

    expected = {"Summary": None, "Active Developers": ACTIVE_DEV_COLS,
                "Developer Summary": DEV_SUMMARY_COLS, "Repository Summary": REPO_SUMMARY_COLS,
                "Commit Details": COMMIT_COLS, "Repository Status": STATUS_COLS,
                "Processing Errors": ERROR_COLS, "Integrity Checks": INTEGRITY_COLS}
    for name, cols in expected.items():
        if name not in wb.sheetnames:
            problems.append(f"Missing worksheet '{name}'.")
        elif cols and headers(wb[name])[:len(cols)] != cols:
            problems.append(f"Unexpected columns in '{name}'.")
    if problems:
        return problems

    def table(ws):
        rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[0] is None:
                break
            rows.append(row)
        return rows

    def count_rows(ws):
        n = 0
        for row in ws.iter_rows(min_row=2, max_col=1, values_only=True):
            if not row or row[0] is None:
                break
            n += 1
        return n

    def col(ws, rows, header):
        idx = headers(ws).index(header)
        return [r[idx] for r in rows]

    total = 0
    for n in wb.sheetnames:
        if n.startswith("Commit Details"):
            total += count_rows(wb[n])
    act, dev, rep = (table(wb[n]) for n in ("Active Developers", "Developer Summary",
                                            "Repository Summary"))
    status = table(wb["Repository Status"])
    s = {}
    for row in wb["Summary"].iter_rows(values_only=True):
        if row and len(row) > 1 and row[0] is not None and row[1] is not None:
            s.setdefault(row[0], row[1])
    chk = lambda cond, msg: None if cond else problems.append(msg)      # noqa: E731
    chk(sum(col(wb["Active Developers"], act, "Total Commits")) == total,
        "Active Developers commit total != Commit Details rows.")
    chk(sum(col(wb["Developer Summary"], dev, "Total Commits")) == total,
        "Developer Summary commit total != Commit Details rows.")
    chk(sum(col(wb["Repository Summary"], rep, "Total Commits")) == total,
        "Repository Summary commit total != Commit Details rows.")
    chk(len(act) == len(dev) == s.get("Unique active developers"),
        "Active developer counts disagree between sheets and Summary.")
    chk(s.get("Total commits") == total, "Summary total commits != Commit Details rows.")
    chk(len(status) == s.get("Repositories discovered"),
        "Repository Status rows != Summary repositories discovered.")
    names = col(wb["Repository Status"], status, "Repository Full Name")
    chk(len(set(names)) == len(names), "A repository appears more than once in Repository Status.")
    states = col(wb["Repository Status"], status, "Processing Status")
    for label, st in (("Successfully analysed", STATUS_ANALYSED), ("Excluded", STATUS_EXCLUDED),
                      ("Skipped", STATUS_SKIPPED), ("Failed", STATUS_FAILED),
                      ("Cancelled / not processed", STATUS_CANCELLED)):
        chk(states.count(st) == s.get(label), f"Summary '{label}' != Repository Status count.")
    chk(sum(states.count(x) for x in ALL_STATUSES) == len(status),
        "Repository Status contains an unknown status value.")
    chk(sum(col(wb["Repository Status"], status, "Commits Found")) == total,
        "Repository Status commits found != Commit Details rows.")
    chk(len(rep) == states.count(STATUS_ANALYSED),
        "Repository Summary rows != analysed repositories.")
    return problems


# ============================================================
# 5. BACKGROUND WORKER + GUI
# ============================================================
def run_analysis_worker(config, put, cancel_event, analyzer_cls=None):
    """Runs in a background thread. Communicates ONLY through put(); never touches Tk."""
    analyzer_cls = analyzer_cls or GitHubAnalyzer
    token = config.get("token", "")
    try:
        analyzer = analyzer_cls(
            days=config["days"], token=token, include_forks=config["include_forks"],
            include_archived=config["include_archived"],
            include_disabled=config["include_disabled"], exclude_bots=config["exclude_bots"],
            exclude_merges=config["exclude_merges"], skip_inactive=config["skip_inactive"],
            date_basis=config.get("date_basis", "author"),
            log_callback=lambda m: put(("log", m)),
            progress_callback=lambda c, t: put(("progress", c, t)),
            cancel_event=cancel_event)
        put(("done", analyzer.analyse(config["url"])))
    except CancelledError:
        put(("failure", "Analysis cancelled before any data could be reported."))
    except AppError as exc:
        file_logger.error(redact(f"{exc.category}: {exc}", (token,)))
        text = f"{exc.category}: {redact(exc, (token,))}"
        put(("failure", text + (f"\n\nSuggested action: {exc.hint}" if exc.hint else "")))
    except ValueError as exc:
        put(("failure", redact(exc, (token,))))
    except Exception as exc:                                    # noqa: BLE001
        file_logger.error(redact(traceback.format_exc(), (token,)))
        put(("failure", f"Unexpected error ({type(exc).__name__}): {redact(exc, (token,))}\n"
                        "Details were written to the session log."))


def format_completion_message(result):
    s = result["stats"]
    lines = [result["outcome"], "",
             f"Repositories discovered: {s['discovered']}",
             f"   = {s['analysed']} analysed + {s['excluded']} excluded + {s['skipped']} skipped"
             f" + {s['failed']} failed + {s['cancelled']} cancelled/not processed"
             f"  [{'reconciled' if s['reconciled'] else 'NOT RECONCILED'}]",
             f"Unique active developers: {s['developers']}",
             f"Total commits: {s['commits']}",
             f"Activity span: {s['span']} day(s); commits on {s['active_days']} of them "
             f"({s['inactive_days']} days without commits)",
             f"Date basis: {s['date_basis']} date (UTC)"]
    failed = result.get("failed") or []
    if failed:
        lines.append("")
        lines.append("Failed repositories (details: 'Processing Errors' sheet):")
        for name, reason in failed[:5]:
            lines.append(f"  - {name}: {reason[:140]}")
        if len(failed) > 5:
            lines.append(f"  ... and {len(failed) - 5} more")
    if s.get("integrity_failures"):
        lines.append(f"INTEGRITY CHECK FAILURES: {s['integrity_failures']} (see report)")
    if s.get("validation_problems"):
        lines.append("Workbook validation problems: " + "; ".join(s["validation_problems"]))
    if result.get("warnings"):
        lines.append("Warnings: " + " | ".join(result["warnings"][:3]))
    lines += ["", "This report is a point-in-time snapshot, not continuous monitoring.", "",
              f"Report:\n{result['report']}"]
    level = "info" if result["outcome"].startswith("Fully") and not s.get(
        "validation_problems") else "warning"
    return ("Analysis Completed" if level == "info" else "Analysis Completed - Review Needed",
            "\n".join(lines), level)


class GitHubActivityApp:

    MAX_LOG_LINES = 5000
    MAX_MESSAGES_PER_TICK = 400

    def __init__(self, root):
        self.root = root
        root.title(f"{APP_TITLE} v{APP_VERSION}")
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1050, sw - 80), min(860, sh - 100)
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max((sh - h) // 3, 0)}")
        root.minsize(780, 600)
        self.message_queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.worker_running = False
        self.report_file = None
        self.determinate = False
        setup_file_logging()
        self.setup_style()
        self.create_widgets()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self.on_tk_exception
        root.after(100, self.process_queue)

    def setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Hint.TLabel", font=("Segoe UI", 9), foreground="gray40")
        style.configure("Action.TButton", font=("Segoe UI", 10, "bold"), padding=8)
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("TCheckbutton", font=("Segoe UI", 10))
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))

    def create_widgets(self):
        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)
        ttk.Label(main, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")

        src = ttk.LabelFrame(main, text="1. GitHub Source", padding=10)
        src.grid(row=1, column=0, sticky="ew", pady=4)
        src.columnconfigure(0, weight=1)
        self.url_var = tk.StringVar()
        entry = ttk.Entry(src, textvariable=self.url_var, font=("Segoe UI", 10))
        entry.grid(row=0, column=0, sticky="ew")
        entry.bind("<Return>", lambda _e: self.start_analysis())
        entry.focus_set()
        ttk.Label(src, style="Hint.TLabel", text=(
            "Accepted: github.com/username  |  github.com/orgs/name/repositories  |  "
            "github.com/owner/repository")).grid(row=1, column=0, sticky="w", pady=(4, 0))

        st = ttk.LabelFrame(main, text="2. Analysis Settings", padding=10)
        st.grid(row=2, column=0, sticky="ew", pady=4)
        st.columnconfigure(1, weight=1)
        ttk.Label(st, text="Reporting period (days):").grid(row=0, column=0, sticky="w",
                                                            padx=(0, 8), pady=3)
        self.days_var = tk.StringVar(value=str(DEFAULT_DAYS))
        ttk.Entry(st, textvariable=self.days_var, width=10).grid(row=0, column=1, sticky="w")
        ttk.Label(st, text="Date basis:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.basis_var = tk.StringVar(value="Author date")
        ttk.Combobox(st, textvariable=self.basis_var, state="readonly", width=18,
                     values=("Author date", "Committer date")).grid(row=1, column=1, sticky="w")
        ttk.Label(st, text="GitHub API token (optional):").grid(row=2, column=0, sticky="w",
                                                                padx=(0, 8), pady=3)
        self.token_var = tk.StringVar()
        ttk.Entry(st, textvariable=self.token_var, show="*").grid(row=2, column=1, sticky="ew")
        ttk.Label(st, style="Hint.TLabel", wraplength=900, justify="left", text=(
            "A token is optional for public repositories but raises the API limit from 60 to "
            "5,000 requests/hour and is required for private repositories. It is only sent to "
            "GitHub and never written to logs or reports. Each run is a point-in-time "
            "snapshot, not continuous monitoring.")).grid(row=3, column=0, columnspan=2,
                                                          sticky="w", pady=(4, 0))

        fl = ttk.LabelFrame(main, text="3. Repository & Commit Filters", padding=10)
        fl.grid(row=3, column=0, sticky="ew", pady=4)
        self.forks_var = tk.BooleanVar(value=False)
        self.archived_var = tk.BooleanVar(value=True)
        self.disabled_var = tk.BooleanVar(value=False)
        self.bots_var = tk.BooleanVar(value=False)
        self.merges_var = tk.BooleanVar(value=False)
        self.inactive_var = tk.BooleanVar(value=False)
        for text, var, r, c in (
                ("Include forks", self.forks_var, 0, 0),
                ("Include archived", self.archived_var, 0, 1),
                ("Include disabled", self.disabled_var, 0, 2),
                ("Exclude bot accounts", self.bots_var, 1, 0),
                ("Exclude merge commits", self.merges_var, 1, 1),
                ("Skip repos with no push in period (faster)", self.inactive_var, 1, 2)):
            ttk.Checkbutton(fl, text=text, variable=var).grid(row=r, column=c, sticky="w",
                                                              padx=(0, 22), pady=2)

        ap = ttk.Frame(main)
        ap.grid(row=4, column=0, sticky="ew", pady=6)
        ap.columnconfigure(0, weight=1)
        bt = ttk.Frame(ap)
        bt.grid(row=0, column=0, columnspan=2, sticky="w")
        self.start_button = ttk.Button(bt, text="START ANALYSIS", style="Action.TButton",
                                       command=self.start_analysis)
        self.start_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(bt, text="CANCEL ANALYSIS", state="disabled",
                                        command=self.cancel_analysis)
        self.cancel_button.pack(side="left", padx=4)
        self.report_button = ttk.Button(bt, text="OPEN REPORT", state="disabled",
                                        command=self.open_report)
        self.report_button.pack(side="left", padx=4)
        ttk.Button(bt, text="OPEN OUTPUT FOLDER", command=self.open_output).pack(
            side="left", padx=4)
        ttk.Button(bt, text="CLEAR LOG", command=self.clear_log).pack(side="left", padx=4)
        self.status_var = tk.StringVar(value="Ready. Enter a GitHub URL to begin.")
        ttk.Label(ap, textvariable=self.status_var).grid(row=1, column=0, columnspan=2,
                                                         sticky="w", pady=(8, 2))
        self.progress_bar = ttk.Progressbar(ap, mode="determinate", maximum=100)
        self.progress_bar.grid(row=2, column=0, sticky="ew")
        self.progress_text = tk.StringVar(value="0%")
        ttk.Label(ap, textvariable=self.progress_text, width=7, anchor="e").grid(
            row=2, column=1, padx=(8, 0))

        lg = ttk.LabelFrame(main, text="4. Execution Log", padding=8)
        lg.grid(row=5, column=0, sticky="nsew", pady=(4, 0))
        lg.columnconfigure(0, weight=1)
        lg.rowconfigure(0, weight=1)
        self.log_text = tk.Text(lg, wrap="word", font=("Consolas", 9), height=8,
                                state="disabled", background="#F7F9FC")
        sb = ttk.Scrollbar(lg, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.append_log(f"Welcome to {APP_TITLE} v{APP_VERSION}. Reports are saved in: {OUTPUT_DIR}")
        self.append_log(f"Git: {describe_git_runtime()}")
        self.append_log(f"Logs: {_log_folder or LOGS_DIR}   |   Repository cache: {REPOSITORIES_DIR}")

    # -- helpers (main thread only) --
    def append_log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{datetime.now():%H:%M:%S}] {message}\n")
        try:                                   # keep the widget small so the GUI stays fast
            if int(self.log_text.index("end-1c").split(".")[0]) > self.MAX_LOG_LINES:
                self.log_text.delete("1.0", f"{self.MAX_LOG_LINES // 5}.0")
        except (tk.TclError, ValueError):
            pass
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def set_running(self, running):
        self.worker_running = running
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")
        if not running:
            self.progress_bar.stop()

    def open_report(self):
        if self.report_file and Path(self.report_file).exists():
            open_path(self.report_file)

    def open_output(self):
        try:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Output folder", f"Cannot create {OUTPUT_DIR}: {exc}")
            return
        open_path(OUTPUT_DIR)

    def on_tk_exception(self, exc, val, tb):
        text = redact("".join(traceback.format_exception(exc, val, tb)), (self.token_var.get(),))
        file_logger.error(text)
        self.append_log("UNEXPECTED GUI ERROR: " + str(val))
        messagebox.showerror("Unexpected error", f"{val}\n\nDetails were written to the log.")

    def on_close(self):
        if self.worker_running:
            if not messagebox.askyesno("Analysis running", "An analysis is running. Cancel it "
                                       "and exit? No report will be written."):
                return
            self.cancel_event.set()
            if self.worker is not None:          # let Git child processes be stopped cleanly
                self.worker.join(timeout=8)
        self.root.destroy()

    def start_analysis(self):
        if self.worker_running:
            return
        url = self.url_var.get().strip()
        try:
            GitHubAnalyzer.parse_github_url(url)
        except ValueError as exc:
            messagebox.showerror("Invalid GitHub URL", str(exc))
            return
        try:
            days = int(self.days_var.get().strip())
            if not 1 <= days <= MAX_DAYS:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Reporting Period",
                                 f"Enter a whole number between 1 and {MAX_DAYS}.")
            return
        if resolve_git()["source"] == "missing":
            if FROZEN:
                messagebox.showerror(
                    "Git Not Found", "The bundled Git (PortableGit) folder is missing or "
                    "incomplete.\nExtract the complete application ZIP again and keep the "
                    "folder structure intact.\n\nSee the session log for details.")
            else:
                messagebox.showerror("Git Not Found", "Git is not installed or not on PATH.\n"
                                     "Install it from https://git-scm.com/downloads")
            return
        self.cancel_event.clear()
        self.report_file = None
        self.report_button.configure(state="disabled")
        self.set_running(True)
        self.determinate = False
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start(15)
        self.progress_text.set("...")
        self.status_var.set("Discovering repositories...")
        self.append_log("=" * 65)
        self.append_log("NEW ANALYSIS STARTED (point-in-time snapshot)")
        config = {"url": url, "days": days, "token": self.token_var.get().strip(),
                  "include_forks": self.forks_var.get(),
                  "include_archived": self.archived_var.get(),
                  "include_disabled": self.disabled_var.get(),
                  "exclude_bots": self.bots_var.get(), "exclude_merges": self.merges_var.get(),
                  "skip_inactive": self.inactive_var.get(),
                  "date_basis": "committer" if self.basis_var.get().startswith("Committer")
                  else "author"}
        self.worker = threading.Thread(
            target=run_analysis_worker,
            args=(config, self.message_queue.put, self.cancel_event), daemon=True)
        self.worker.start()

    def cancel_analysis(self):
        if self.worker_running:
            self.cancel_event.set()
            self.status_var.set("Cancelling... (a partial report will be saved)")

    def process_queue(self):
        alive = self.worker is not None and self.worker.is_alive()
        finished = False
        try:
            # bounded batch per tick: a burst of log lines can never freeze the window
            for _ in range(self.MAX_MESSAGES_PER_TICK):
                item = self.message_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.append_log(item[1])
                elif kind == "progress":
                    if not self.determinate:
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate")
                        self.determinate = True
                    cur, tot = item[1], item[2]
                    pct = cur / tot * 100 if tot else 100
                    self.progress_bar.configure(value=pct)
                    self.progress_text.set(f"{pct:.0f}%")
                    self.status_var.set(f"Processed {cur} of {tot} selected repositories")
                elif kind == "done":
                    finished = True
                    result = item[1]
                    self.report_file = Path(result["report"])
                    self.set_running(False)
                    self.progress_bar.configure(mode="determinate", value=100)
                    self.report_button.configure(state="normal")
                    self.status_var.set(result["outcome"])
                    title, text, level = format_completion_message(result)
                    (messagebox.showinfo if level == "info" else messagebox.showwarning)(
                        title, text)
                elif kind == "failure":
                    finished = True
                    self.set_running(False)
                    self.status_var.set("Analysis could not be completed.")
                    self.append_log("FATAL ERROR: " + item[1])
                    messagebox.showerror("Analysis Failed", item[1])
        except queue.Empty:
            pass
        if self.worker_running and not alive and not finished:
            self.set_running(False)          # worker died without reporting
            self.status_var.set("Analysis stopped unexpectedly.")
            messagebox.showerror("Analysis Failed", "The analysis thread ended unexpectedly. "
                                 f"See the session log in: {_log_folder or LOGS_DIR}")
        self.root.after(50, self.process_queue)


# ============================================================
# 6. SELF-TEST, BUILD-FILE EMITTER, ENTRY POINT
# ============================================================
def run_self_test(online=False):
    """Verify the packaged runtime on the machine it runs on.  Writes a report into the
    log folder and shows a summary box (works in a console-less EXE).  Returns 0/1."""
    results = []

    def record(ok, name, detail=""):
        results.append(("PASS" if ok else "FAIL", name, str(detail)))

    def note(name, detail=""):
        results.append(("INFO", name, str(detail)))

    setup_file_logging()
    note("Application", f"{APP_TITLE} v{APP_VERSION}")
    note("Runtime", f"frozen={FROZEN}; python {sys.version.split()[0]}; {sys.platform}; "
                    f"{'64' if sys.maxsize > 2 ** 32 else '32'}-bit")
    note("Executable", sys.executable)
    note("Resource folder (read-only)", RESOURCE_DIR)
    for label, folder in (("Output folder", OUTPUT_DIR), ("Log folder", _log_folder or LOGS_DIR),
                          ("Repository cache", REPOSITORIES_DIR)):
        record(_writable_dir(folder), f"{label} is writable", folder)

    for module in ("pandas", "numpy", "requests", "urllib3", "openpyxl", "certifi", "ssl",
                   "sqlite3", "tkinter"):
        try:
            mod = importlib.import_module(module)
            record(True, f"import {module}", getattr(mod, "__version__", ""))
        except Exception as exc:                              # noqa: BLE001
            record(False, f"import {module}", f"{type(exc).__name__}: {exc}")
    try:
        import certifi
        record(Path(certifi.where()).is_file(), "certifi CA bundle present", certifi.where())
    except Exception as exc:                                  # noqa: BLE001
        record(False, "certifi CA bundle present", exc)

    if tk is not None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.update_idletasks()
            root.destroy()
            record(True, "Tk window can be created")
        except Exception as exc:                              # noqa: BLE001
            record(False, "Tk window can be created", exc)
    else:
        record(False, "Tk window can be created", "tkinter not available")

    scratch = Path(tempfile.mkdtemp(prefix="gha_selftest_"))
    try:
        try:
            from openpyxl import Workbook as _WB
            wb = _WB()
            wb.active.append(["a", "b"])
            wb.save(scratch / "t.xlsx")
            pd.DataFrame({"x": [1, 2]}).to_excel(scratch / "p.xlsx", index=False)
            ok = (load_workbook(scratch / "t.xlsx").active["A1"].value == "a"
                  and load_workbook(scratch / "p.xlsx").active.max_row == 3)
            record(ok, "Excel workbook write/read (openpyxl + pandas)")
        except Exception as exc:                              # noqa: BLE001
            record(False, "Excel workbook write/read (openpyxl + pandas)", exc)

        runtime = resolve_git(force=True)
        record(runtime["source"] != "missing", "Git found", describe_git_runtime(runtime))
        if FROZEN and _is_windows():
            record(runtime["source"] == "bundled", "Application uses the BUNDLED Git",
                   runtime["source"])
        note("Git on host PATH (not required)", shutil.which("git") or "none")
        if runtime["source"] != "missing":
            try:
                an = GitHubAnalyzer(days=1, repos_dir=scratch / "r", output_dir=scratch / "o",
                                    aliases={"x": "x"})
                record(True, "git --version", an.run_git(["--version"], timeout=60).strip())
                src_repo = scratch / "source"
                an.run_git(["init", "--quiet", str(src_repo)], timeout=60)
                an.run_git(["-c", "user.name=Self Test", "-c", "user.email=t@example.com",
                            "commit", "--allow-empty", "--quiet", "-m", "self-test"],
                           cwd=src_repo, timeout=60)
                log = an.run_git(["log", "--all", "--format=%H"], cwd=src_repo, timeout=60)
                record(bool(HASH_RE.match(log.strip())), "git init / commit / log")
                streamed = "".join(an.stream_git(["log", "--all", "--format=%H"],
                                                 cwd=src_repo, timeout=60))
                record(streamed.strip() == log.strip(), "git log streaming")
                clone = scratch / "clone"
                an.run_git(["clone", "--quiet", "--no-checkout", str(src_repo), str(clone)],
                           timeout=120)
                an.verify_local_repository(clone)
                an.run_git(["fetch", "--all", "--prune", "--quiet"], cwd=clone, timeout=60)
                record(True, "git clone / verify / fetch (local)")
            except Exception as exc:                          # noqa: BLE001
                record(False, "Git operations", f"{type(exc).__name__}: {exc}")
        if online:
            try:
                r = requests.get(f"{GITHUB_API}/rate_limit", timeout=REQUEST_TIMEOUT,
                                 headers={"User-Agent": "GitHub-Developer-Activity-Analytics"})
                record(r.status_code in (200, 403), "HTTPS to api.github.com (certificate verified)",
                       f"HTTP {r.status_code}")
            except Exception as exc:                          # noqa: BLE001
                record(False, "HTTPS to api.github.com (certificate verified)",
                       f"{type(exc).__name__}: {exc}")
            if runtime["source"] != "missing":
                try:
                    an = GitHubAnalyzer(days=1, repos_dir=scratch / "r", output_dir=scratch / "o",
                                        aliases={"x": "x"})
                    out = an.run_git(["ls-remote", "--exit-code",
                                      "https://github.com/git/git.git", "HEAD"], timeout=120)
                    record(bool(out.strip()), "git ls-remote over HTTPS (Git's own TLS stack)")
                except Exception as exc:                      # noqa: BLE001
                    record(False, "git ls-remote over HTTPS (Git's own TLS stack)",
                           f"{type(exc).__name__}: {exc}")
        else:
            note("Online checks", "skipped (add --online to test HTTPS)")
    finally:
        force_rmtree(scratch)

    failed = [r for r in results if r[0] == "FAIL"]
    lines = [f"{APP_TITLE} v{APP_VERSION} - self-test {datetime.now():%Y-%m-%d %H:%M:%S}", ""]
    lines += [f"[{status}] {name}" + (f": {detail}" if detail else "")
              for status, name, detail in results]
    lines += ["", "RESULT: " + ("ALL CHECKS PASSED" if not failed
                               else f"{len(failed)} CHECK(S) FAILED")]
    report = "\n".join(lines)
    report_path = None
    for folder in (_log_folder, LOGS_DIR, Path(tempfile.gettempdir())):
        try:
            if folder is None:
                continue
            Path(folder).mkdir(parents=True, exist_ok=True)
            report_path = Path(folder) / f"selftest_{datetime.now():%Y%m%d_%H%M%S}.txt"
            report_path.write_text(report, encoding="utf-8")
            break
        except OSError:
            report_path = None
    safe_print(report)
    if _is_windows() or not sys.stdout:          # a console-less EXE needs a visible result
        show_message(f"{APP_TITLE} v{APP_VERSION} - Self-test",
                     report + (f"\n\nSaved to: {report_path}" if report_path else ""),
                     error=bool(failed))
    return 1 if failed else 0


_SPEC_TEMPLATE = r"""# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for @TITLE@ v@VERSION@ (written by --emit-build-files).
import os
import sys
from PyInstaller.utils.hooks import collect_all

APP_NAME = "@APP@"
HERE = os.path.abspath(SPECPATH)
SCRIPT = os.path.join(HERE, "@SCRIPT@")
GIT_DIR = os.path.join(HERE, "PortableGit")
ARCH_DIRS = ("ucrt64", "mingw64", "clang64", "clangarm64")


def require(condition, message):
    if not condition:
        raise SystemExit("BUILD STOPPED: " + message)


require(os.path.isfile(SCRIPT), "script not found: " + SCRIPT)
require(os.path.isdir(GIT_DIR), "PortableGit folder not found: " + GIT_DIR)
require(not os.path.exists(os.path.join(GIT_DIR, "post-install.bat")),
        "PortableGit post-install.bat has not been run yet (see build instructions).")
require(any(os.path.isfile(os.path.join(GIT_DIR, a, "bin", "git.exe"))
            and os.path.isfile(os.path.join(GIT_DIR, a, "libexec", "git-core",
                                            "git-remote-https.exe"))
            for a in ARCH_DIRS),
        "PortableGit is incomplete (git.exe / git-remote-https.exe not found).")

datas, binaries, hiddenimports = [], [], []
for package in ("certifi", "openpyxl", "et_xmlfile"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += ["tkinter", "tkinter.ttk", "tkinter.messagebox", "sqlite3"]

# The COMPLETE PortableGit tree, with its folder structure preserved, as plain data
# files (PyInstaller must not rewrite or strip Git's own executables and DLLs).
for folder, _dirs, files in os.walk(GIT_DIR):
    destination = os.path.relpath(folder, HERE)
    for name in files:
        datas.append((os.path.join(folder, name), destination))

a = Analysis(
    [SCRIPT], pathex=[HERE], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[], runtime_hooks=[], noarchive=False,
    excludes=["matplotlib", "scipy", "IPython", "pytest", "numba", "pyarrow", "lxml",
              "sqlalchemy", "tables", "jinja2"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME,
    debug=False, strip=False, upx=False,
    console=False,                       # GUI application: no Command Prompt window
    version=(os.path.join(HERE, "version_info.txt") if sys.platform == "win32" else None),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
"""

_VERSION_TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(@MAJOR@, @MINOR@, @PATCH@, 0),
    prodvers=(@MAJOR@, @MINOR@, @PATCH@, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', ''),
      StringStruct('FileDescription', '@TITLE@'),
      StringStruct('FileVersion', '@VERSION@'),
      StringStruct('InternalName', '@APP@'),
      StringStruct('OriginalFilename', '@APP@.exe'),
      StringStruct('ProductName', '@TITLE@'),
      StringStruct('ProductVersion', '@VERSION@')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ])
"""


def emit_build_files(target=None):
    """Write the PyInstaller .spec and Windows version resource (build-time helper)."""
    if FROZEN:
        raise SystemExit("--emit-build-files must be run from the Python source file.")
    folder = Path(target or Path.cwd()).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    major, minor, patch = (int(p) for p in APP_VERSION.split("."))
    values = {"@TITLE@": APP_TITLE, "@VERSION@": APP_VERSION, "@APP@": APP_FOLDER_NAME,
              "@SCRIPT@": Path(__file__).name, "@MAJOR@": str(major),
              "@MINOR@": str(minor), "@PATCH@": str(patch)}
    written = []
    for name, template in ((f"{APP_FOLDER_NAME}.spec", _SPEC_TEMPLATE),
                           ("version_info.txt", _VERSION_TEMPLATE)):
        text = template
        for key, value in values.items():
            text = text.replace(key, value)
        (folder / name).write_text(text, encoding="utf-8")
        written.append(str(folder / name))
    return written


def run_gui():
    if tk is None:
        show_message(f"{APP_TITLE} v{APP_VERSION}",
                     "Tkinter is not available in this Python installation.", error=True)
        return
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    GitHubActivityApp(root)
    root.mainloop()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    install_exception_hooks()
    if "--version" in args:
        show_message(APP_TITLE, f"{APP_TITLE} v{APP_VERSION}")
        return 0
    if "--emit-build-files" in args:
        index = args.index("--emit-build-files")
        target = args[index + 1] if index + 1 < len(args) and not args[index + 1].startswith("-") else None
        for path in emit_build_files(target):
            safe_print(f"written: {path}")
        return 0
    if "--self-test" in args:
        return run_self_test(online="--online" in args)
    try:
        setup_file_logging()
        run_gui()
    except Exception as exc:                                   # noqa: BLE001
        file_logger.critical(redact(traceback.format_exc()))
        show_message(f"{APP_TITLE} v{APP_VERSION} - Startup failed",
                     f"{redact(exc)}\n\nDetails were written to:\n{_log_folder or LOGS_DIR}",
                     error=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
