# main.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import sys
import json
import time
import shutil
import sqlite3
import contextlib
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# ============================ App Config ============================

APP_NAME = "Steamer"
DB_NAME = "games.db"
ARCHIVE_DIRNAME = "gamesAdded"

# SteamDB / scraper settings (used in the worker subprocess)
HEADLESS: bool = True
USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
PAGE_LOAD_TIMEOUT_SECONDS: int = 60
CLOUDFLARE_WAIT_SECONDS: int = 35
TABLE_WAIT_SECONDS: int = 20

# Optional: set Cloudflare-related cookies if your region enforces strict checks
COOKIES: Dict[str, str] = {
    # "cf_clearance": "<paste if needed>",
    # "__cf_bm": "<optional>",
}

STEAMDB_ORIGIN = "https://steamdb.info"
STEAMDB_DEPOT_URL = "https://steamdb.info/depot/{depot}/manifests/"

# App icon (optional): place steamer.ico next to this file
ICON_FILE = "steamer.ico"

# ============================ Icon Helpers ============================

from PyQt6 import QtGui  # noqa: E402


def resource_path(rel: str) -> Path:
    """Supports normal execution and PyInstaller bundles."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


def get_app_icon() -> QtGui.QIcon:
    p = resource_path(ICON_FILE)
    return QtGui.QIcon(str(p)) if p.exists() else QtGui.QIcon()


# ============================ Core (shared) logic ============================

# Windows registry (for Steam path auto-detection)
try:
    import winreg  # type: ignore
except Exception:
    winreg = None  # Fallback paths will be used


def find_steam_root() -> Path:
    """
    Detect the Steam install directory on Windows (registry + common fallbacks).
    """
    candidates: List[Path] = []

    if winreg is not None:
        for value_name in ("SteamPath", "InstallPath"):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", 0, winreg.KEY_READ
                ) as k:
                    val, _ = winreg.QueryValueEx(k, value_name)
                    if val:
                        candidates.append(Path(val))
            except OSError:
                pass

        try:
            access = winreg.KEY_READ
            if hasattr(winreg, "KEY_WOW64_32KEY"):
                access |= winreg.KEY_WOW64_32KEY
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", 0, access
            ) as k:
                val, _ = winreg.QueryValueEx(k, "InstallPath")
                if val:
                    candidates.append(Path(val))
        except OSError:
            pass

    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.extend(
        [
            Path(pf86) / "Steam",
            Path(pf) / "Steam",
            Path.home() / "AppData" / "Local" / "Steam",
        ]
    )

    for p in candidates:
        if (p / "steam.exe").exists() or (p / "config").exists():
            return p.resolve()

    raise FileNotFoundError("Steam root not found.")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def backup_then_move(src: Path, dest_dir: Path) -> Path:
    """
    Move src into dest_dir. If target exists, rename it to *.backup / *.backup.N first.
    Returns the final target path inside dest_dir.
    """
    target = dest_dir / src.name
    if target.exists():
        backup = dest_dir / f"{src.stem}.backup{target.suffix}"
        idx = 1
        while backup.exists():
            backup = dest_dir / f"{src.stem}.backup.{idx}{target.suffix}"
            idx += 1
        target.replace(backup)
    shutil.move(str(src), str(dest_dir))
    return dest_dir / src.name


def archive_final_copy(src_final_path: Path, archive_dir: Path) -> Path:
    """
    Copy the FINAL Lua file (after rewrite) into archive_dir with a timestamped name.
    """
    ensure_dir(archive_dir)
    ts = time.strftime("%Y%m%d_%H%M%S")
    archive_name = f"{src_final_path.stem}.{ts}{src_final_path.suffix}"
    archive_path = archive_dir / archive_name
    shutil.copy2(str(src_final_path), str(archive_path))
    return archive_path


RE_ADDAPPID_TOKEN = re.compile(
    r'addappid\(\s*(\d+)\s*,\s*1\s*,\s*"([^"]+)"\s*\)', re.IGNORECASE
)
RE_SETMANIFEST = re.compile(
    r'setManifestid\(\s*(\d+)\s*,\s*"(\d+)"\s*,\s*0\s*\)', re.IGNORECASE
)
RE_ADDAPPID_SINGLE = re.compile(r'addappid\(\s*(\d+)\s*\)(?!\s*,)', re.IGNORECASE)


def infer_appid_from_filename(path: Path) -> int:
    m = re.search(r"\d+", path.stem)
    if not m:
        raise ValueError(f"Cannot infer APPID from filename: {path.name}")
    return int(m.group(0))


def parse_from_content(text: str) -> Dict[str, object]:
    """
    Extract appid (single-arg addappid), depot/token (multi-arg addappid),
    and depot/manifest_id (setManifestid).
    """
    out: Dict[str, object] = {}

    m = RE_ADDAPPID_SINGLE.search(text)
    if m:
        out["appid"] = int(m.group(1))

    m = RE_ADDAPPID_TOKEN.search(text)
    if m:
        out["depot"] = int(m.group(1))
        out["token"] = m.group(2)

    m = RE_SETMANIFEST.search(text)
    if m:
        out["depot"] = int(m.group(1))
        out["manifest_id"] = m.group(2)

    return out


def load_sidecar_json(stem: str, folder: Path) -> Dict[str, object]:
    """
    Read <stem>.json next to the Lua file if present.
    Expected keys: depot (int), token (str), manifest_id (str), appid (int, optional).
    """
    cfg = folder / f"{stem}.json"
    if not cfg.exists():
        return {}
    try:
        with cfg.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        sanitized: Dict[str, object] = {}
        if "appid" in data:
            sanitized["appid"] = int(data["appid"])
        if "depot" in data:
            sanitized["depot"] = int(data["depot"])
        if "token" in data:
            sanitized["token"] = str(data["token"])
        if "manifest_id" in data:
            sanitized["manifest_id"] = str(data["manifest_id"])
        return sanitized
    except Exception:
        return {}


def build_lua_content(appid: int, depot: int, token: str, manifest_id: str) -> str:
    return (
        f"addappid({appid})\n"
        f'addappid({depot},1,"{token}")\n'
        f'setManifestid({depot},"{manifest_id}",0)\n'
    )


# -------------------- SQLite --------------------

def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gamesAdded (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            appid INTEGER NOT NULL,
            depot INTEGER NOT NULL,
            manifest_id TEXT NOT NULL,
            dest_path TEXT NOT NULL,
            moved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    return conn


def record_in_db(
    conn: sqlite3.Connection, filename: str, appid: int, depot: int, manifest_id: str, dest_path: Path
) -> None:
    conn.execute(
        "INSERT INTO gamesAdded (filename, appid, depot, manifest_id, dest_path) VALUES (?, ?, ?, ?, ?);",
        (filename, appid, depot, manifest_id, str(dest_path)),
    )
    conn.commit()


def load_latest_rows(conn: sqlite3.Connection) -> List[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT filename, appid, depot, manifest_id, dest_path, moved_at
        FROM gamesAdded
        ORDER BY moved_at ASC
        """
    )
    rows = cur.fetchall()
    latest: Dict[Tuple[str, int], dict] = {}
    for filename, appid, depot, manifest_id, dest_path, moved_at in rows:
        latest[(filename, int(depot))] = {
            "filename": filename,
            "appid": int(appid),
            "depot": int(depot),
            "manifest_id": str(manifest_id),
            "dest_path": str(dest_path),
            "moved_at": str(moved_at),
        }
    return list(latest.values())


def update_db_manifest(conn: sqlite3.Connection, filename: str, depot: int, new_manifest: str) -> None:
    cur = conn.cursor()
    cur.execute(
        "UPDATE gamesAdded SET manifest_id = ? WHERE filename = ? AND depot = ?",
        (new_manifest, filename, depot),
    )
    conn.commit()


def update_lua_manifest(file_path: Path, depot: int, new_manifest: str) -> bool:
    """
    Replace setManifestid(<depot>,"OLD",0) with setManifestid(<depot>,"NEW",0)
    using a safe regex (no backref confusion).
    """
    if not file_path.exists():
        return False
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    pattern = re.compile(
        rf'(?im)^(\s*setManifestid\(\s*{depot}\s*,\s*")(\d+)("\s*,\s*0\s*\)\s*)$'
    )

    def repl(m: re.Match) -> str:
        return f'{m.group(1)}{new_manifest}{m.group(3)}'

    new_text, n = pattern.subn(repl, text)
    if n == 0:
        pattern2 = re.compile(
            rf'(setManifestid\(\s*{depot}\s*,\s*")(\d+)("\s*,\s*0\s*\))',
            re.IGNORECASE,
        )
        new_text, n = pattern2.subn(repl, text, count=1)
        if n == 0:
            return False

    try:
        file_path.write_text(new_text, encoding="utf-8")
        check = re.search(
            rf'setManifestid\(\s*{depot}\s*,\s*"{re.escape(new_manifest)}"\s*,\s*0\s*\)',
            new_text,
            re.IGNORECASE,
        )
        return bool(check)
    except Exception:
        return False


# ============================ Worker Subprocess (CLI mode) ============================

def run_check_worker_cli() -> None:
    """
    Child process: performs Selenium work and prints updates JSON to stdout.
    Progress logs are sent to stderr so the GUI can stream them live.
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args, _ = parser.parse_known_args()  # ignore unknown flags
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[ERROR] Database not found at {db_path}", file=sys.stderr, flush=True)
        print("[]")
        return

    # Heavy imports inside the child only
    from bs4 import BeautifulSoup  # noqa: WPS433
    import undetected_chromedriver as uc  # noqa: WPS433
    from selenium.webdriver.common.by import By  # noqa: WPS433
    from selenium.webdriver.support.ui import WebDriverWait  # noqa: WPS433
    from selenium.webdriver.support import expected_conditions as EC  # noqa: WPS433
    from selenium.common.exceptions import TimeoutException  # noqa: WPS433

    try:
        uc.Chrome.__del__ = lambda self: None  # type: ignore[attr-defined]
    except Exception:
        pass

    def make_driver():
        def _opts(use_new: bool):
            o = uc.ChromeOptions()
            if HEADLESS:
                o.add_argument("--headless=new" if use_new else "--headless")
            o.add_argument(f"--user-agent={USER_AGENT}")
            o.add_argument("--disable-gpu")
            o.add_argument("--no-sandbox")
            o.add_argument("--disable-dev-shm-usage")
            o.add_argument("--no-first-run")
            o.add_argument("--disable-extensions")
            o.add_argument("--disable-background-networking")
            o.add_argument("--disable-features=TranslateUI")
            o.add_argument("--lang=en-US,en")
            o.add_argument("--disable-blink-features=AutomationControlled")
            return o

        try:
            opts = _opts(True)
            drv = uc.Chrome(options=opts, use_subprocess=True)
        except Exception:
            opts = _opts(False)
            drv = uc.Chrome(options=opts, use_subprocess=True)

        drv.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SECONDS)
        return drv

    def inject_cookies_if_any(driver) -> None:
        if not COOKIES:
            return
        driver.get(STEAMDB_ORIGIN + "/")
        for name, val in COOKIES.items():
            if not val:
                continue
            driver.add_cookie(
                {"name": name, "value": val, "domain": "steamdb.info", "path": "/"}
            )

    def wait_cloudflare(driver) -> None:
        start = time.time()
        while time.time() - start < CLOUDFLARE_WAIT_SECONDS:
            title = (driver.title or "").lower()
            src = driver.page_source.lower()
            if (
                "just a moment" in title
                or "checking your browser" in src
                or "cf-chl" in src
            ):
                time.sleep(1.0)
                continue
            break

    def fetch_html(driver, url: str) -> str:
        driver.get(url)
        wait_cloudflare(driver)
        try:
            WebDriverWait(driver, TABLE_WAIT_SECONDS).until(
                EC.presence_of_element_located(
                    (
                        By.XPATH,
                        "//h2[contains(., 'Previously seen manifests')] | "
                        "//h3[contains(., 'Previously seen manifests')]",
                    )
                )
            )
        except TimeoutException:
            pass
        return driver.page_source

    def parse_latest_manifest_id(html: str) -> Optional[str]:
        soup = BeautifulSoup(html, "html.parser")
        hdr = soup.find(
            lambda t: t.name in ("h2", "h3")
            and "Previously seen manifests" in t.get_text()
        )
        if not hdr:
            return None
        table = hdr.find_next("table")
        if not table:
            return None
        body = table.find("tbody")
        if not body:
            return None
        row = body.find("tr")
        if not row:
            return None
        cells = row.find_all("td")
        if len(cells) < 3:
            return None
        text = cells[2].get_text(" ", strip=True)
        m = re.search(r"\b\d{10,}\b", text)
        return m.group(0) if m else None

    def get_latest_manifest_for_depot(driver, depot: int) -> Optional[str]:
        url = STEAMDB_DEPOT_URL.format(depot=depot)
        html = fetch_html(driver, url)
        return parse_latest_manifest_id(html)

    conn = open_db(db_path)
    rows = load_latest_rows(conn)
    if not rows:
        print("[INFO] gamesAdded is empty.", file=sys.stderr, flush=True)
        print("[]")
        conn.close()
        return

    print("[INFO] Starting headless check…", file=sys.stderr, flush=True)
    driver = None
    to_update: List[dict] = []
    try:
        driver = make_driver()
        inject_cookies_if_any(driver)
        checked = errors = 0

        for row in rows:
            checked += 1
            filename = row["filename"]
            depot = row["depot"]
            current_manifest = row["manifest_id"]
            dest_path = Path(row["dest_path"])
            lua_file = dest_path / filename
            try:
                latest_manifest = get_latest_manifest_for_depot(driver, depot)
            except Exception as e:
                print(f"[CHECK ERROR] depot {depot} ({filename}): {e}", file=sys.stderr, flush=True)
                errors += 1
                continue

            if not latest_manifest:
                print(f"[NO DATA] depot {depot} ({filename})", file=sys.stderr, flush=True)
                continue

            if latest_manifest != current_manifest:
                to_update.append(
                    {
                        "filename": filename,
                        "depot": depot,
                        "current_manifest": current_manifest,
                        "latest_manifest": latest_manifest,
                        "lua_path": str(lua_file),
                        "dest_path": str(dest_path),
                    }
                )
                print(
                    f"[UPDATE] {filename} depot {depot}: {current_manifest} -> {latest_manifest}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"[OK] {filename} depot {depot}: up-to-date ({current_manifest})",
                    file=sys.stderr,
                    flush=True,
                )

        print(
            f"[CHECK DONE] checked={checked}, updates={len(to_update)}, errors={errors}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        with contextlib.suppress(Exception):
            if driver is not None:
                driver.quit()
        conn.close()

    # JSON ONLY on stdout
    print(json.dumps(to_update, ensure_ascii=False))


# ============================ GUI (PyQt6) ============================

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

try:
    import qdarktheme  # type: ignore  # noqa: E402
    HAS_QDARKTHEME = True
except Exception:
    HAS_QDARKTHEME = False


class LogBus(QtCore.QObject):
    message = QtCore.pyqtSignal(str)


class DropListWidget(QtWidgets.QListWidget):
    """
    Drag & drop area for .lua files with a centered placeholder text.
    """
    filesDropped = QtCore.pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)

        self.placeholder = "Drag & drop .lua files here\n(or use Browse → Add)"
        self.setMinimumHeight(220)
        self.setStyleSheet(
            """
            QListWidget {
                border: 2px dashed #5a5a5a;
                border-radius: 10px;
                background: transparent;
            }
            """
        )

    # Drag & drop handlers
    def dragEnterEvent(self, e: QtGui.QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e: QtGui.QDragMoveEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e: QtGui.QDropEvent):
        paths: List[str] = []
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.suffix.lower() == ".lua" and p.exists():
                paths.append(str(p))
        if paths:
            self.filesDropped.emit(paths)
        e.acceptProposedAction()

    # Placeholder rendering
    def paintEvent(self, event: QtGui.QPaintEvent):
        super().paintEvent(event)
        if self.count() == 0:
            painter = QtGui.QPainter(self.viewport())
            painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, True)
            painter.setPen(QtGui.QColor(160, 160, 160))
            font = self.font()
            font.setBold(True)
            font.setPointSize(font.pointSize() + 1)
            painter.setFont(font)
            rect = self.viewport().rect().adjusted(12, 12, -12, -12)
            flags = QtCore.Qt.AlignmentFlag.AlignCenter | QtCore.Qt.TextFlag.TextWordWrap
            painter.drawText(rect, flags, self.placeholder)
            painter.end()


class InjectWorker(QtCore.QRunnable):
    """
    Background worker to inject/move .lua files and log to DB.
    """

    def __init__(self, files: List[Path], app_dir: Path, log: LogBus):
        super().__init__()
        self.files = files
        self.app_dir = app_dir
        self.log = log

    @QtCore.pyqtSlot()
    def run(self):
        try:
            steam_root = find_steam_root()
        except Exception as e:
            self.log.message.emit(f"[ERROR] {e}")
            return

        dest_dir = steam_root / "config" / "stplug-in"
        archive_dir = self.app_dir / ARCHIVE_DIRNAME
        ensure_dir(dest_dir)
        ensure_dir(archive_dir)

        conn = open_db(self.app_dir / DB_NAME)
        moved = skipped = errors = archived = 0

        for src in self.files:
            try:
                src = src.resolve()
                if not src.exists() or src.suffix.lower() != ".lua":
                    self.log.message.emit(f"[SKIP] {src.name}: not a .lua file")
                    skipped += 1
                    continue

                try:
                    original_text = src.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    original_text = ""

                parsed = parse_from_content(original_text)
                sidecar = load_sidecar_json(src.stem, src.parent)

                appid = sidecar.get("appid", parsed.get("appid", None))  # type: ignore[arg-type]
                if appid is None:
                    appid = infer_appid_from_filename(src)

                depot = sidecar.get("depot", parsed.get("depot", None))  # type: ignore[arg-type]
                token = sidecar.get("token", parsed.get("token", None))  # type: ignore[arg-type]
                manifest_id = sidecar.get("manifest_id", parsed.get("manifest_id", None))  # type: ignore[arg-type]

                missing = [
                    k
                    for k, v in (("depot", depot), ("token", token), ("manifest_id", manifest_id))
                    if v in (None, "")
                ]
                if missing:
                    self.log.message.emit(
                        f"[SKIP] {src.name}: missing {', '.join(missing)} "
                        f"(provide in file content or {src.stem}.json)"
                    )
                    skipped += 1
                    continue

                final_path = backup_then_move(src, dest_dir)
                new_content = build_lua_content(int(appid), int(depot), str(token), str(manifest_id))
                final_path.write_text(new_content, encoding="utf-8")

                archived_path = archive_final_copy(final_path, archive_dir)
                self.log.message.emit(f"[ARCHIVE] {archived_path.name}")

                record_in_db(conn, final_path.name, int(appid), int(depot), str(manifest_id), dest_dir)
                self.log.message.emit(f"[INJECTED] {final_path.name} -> {dest_dir}")

                moved += 1
                archived += 1

            except PermissionError:
                self.log.message.emit(f"[ERROR] Permission denied: {src.name} (run as Administrator).")
                errors += 1
            except Exception as ex:
                self.log.message.emit(f"[ERROR] {src.name}: {ex}")
                errors += 1

        conn.close()
        self.log.message.emit(
            f"\n[INJECT DONE] moved={moved}, archived={archived}, skipped={skipped}, errors={errors}"
        )


class UpdateApplyWorker(QtCore.QRunnable):
    """
    Background worker to apply discovered ManifestID updates to lua files and DB.
    """

    def __init__(self, app_dir: Path, updates: List[dict], log: LogBus):
        super().__init__()
        self.app_dir = app_dir
        self.updates = updates
        self.log = log

    @QtCore.pyqtSlot()
    def run(self):
        if not self.updates:
            self.log.message.emit("[INFO] No updates to apply.")
            return

        conn = open_db(self.app_dir / DB_NAME)
        succeeded = failed = 0

        for item in self.updates:
            filename = item["filename"]
            depot = int(item["depot"])
            new_manifest = str(item["latest_manifest"])
            lua_path = Path(item["lua_path"])

            ok_file = update_lua_manifest(lua_path, depot, new_manifest)
            ok_db = False

            if ok_file:
                try:
                    update_db_manifest(conn, filename, depot, new_manifest)
                    ok_db = True
                except Exception as e:
                    self.log.message.emit(f"[DB ERROR] {filename} (depot {depot}): {e}")

            if ok_file and ok_db:
                succeeded += 1
                self.log.message.emit(f"[UPDATED] {filename} (depot {depot}) -> manifest {new_manifest}")
            else:
                failed += 1
                self.log.message.emit(f"[FAILED] {filename} (depot {depot})")

        conn.close()
        self.log.message.emit(f"\n[APPLY DONE] updated={succeeded}, failed={failed}")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(980, 640)

        self.app_dir = Path(__file__).resolve().parent
        self.app_icon = get_app_icon()
        if not self.app_icon.isNull():
            self.setWindowIcon(self.app_icon)

        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self.bus = LogBus()
        self.bus.message.connect(self.append_log)

        self._build_ui()
        self._apply_styles()

        self.proc: Optional[QtCore.QProcess] = None
        self._proc_stdout = b""

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Title row with optional icon
        title_row = QtWidgets.QHBoxLayout()
        title_icon = QtWidgets.QLabel()
        if not self.app_icon.isNull():
            title_icon.setPixmap(self.app_icon.pixmap(32, 32))
        title = QtWidgets.QLabel("Steamer , Steam Lua Injector + Updater")
        f = title.font()
        f.setPointSize(18)
        f.setBold(True)
        title.setFont(f)
        title_row.addWidget(title_icon)
        title_row.addWidget(title)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        # File row
        row = QtWidgets.QHBoxLayout()
        self.pathEdit = QtWidgets.QLineEdit()
        self.pathEdit.setPlaceholderText("Select a .lua file…")
        browseBtn = QtWidgets.QPushButton("Browse")
        browseBtn.clicked.connect(self.browse_files)
        addBtn = QtWidgets.QPushButton("Add")
        addBtn.clicked.connect(self.add_from_lineedit)
        clearBtn = QtWidgets.QPushButton("Clear")
        clearBtn.clicked.connect(self.clear_list)
        row.addWidget(self.pathEdit, 1)
        row.addWidget(browseBtn)
        row.addWidget(addBtn)
        row.addWidget(clearBtn)
        layout.addLayout(row)

        # DnD list
        self.dropList = DropListWidget()
        self.dropList.filesDropped.connect(self.add_files)
        layout.addWidget(self.dropList, 1)

        # Buttons
        btnRow = QtWidgets.QHBoxLayout()
        self.injectBtn = QtWidgets.QPushButton("Inject / Move to Steam")
        inject_fallback = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_ArrowForward)
        self.injectBtn.setIcon(self.app_icon if not self.app_icon.isNull() else inject_fallback)
        self.injectBtn.clicked.connect(self.inject_clicked)

        self.checkBtn = QtWidgets.QPushButton("Check Updates")
        check_fallback = self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_BrowserReload)
        self.checkBtn.setIcon(self.app_icon if not self.app_icon.isNull() else check_fallback)
        self.checkBtn.clicked.connect(self.check_updates_clicked)

        # NEW: Find Steam Path button
        self.locateBtn = QtWidgets.QPushButton("Find Steam Path")
        self.locateBtn.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DirIcon))
        self.locateBtn.clicked.connect(self.locate_steam_path)

        openDbBtn = QtWidgets.QPushButton("Open DB Folder")
        openDbBtn.clicked.connect(self.open_db_folder)

        btnRow.addWidget(self.injectBtn)
        btnRow.addWidget(self.checkBtn)
        btnRow.addWidget(self.locateBtn)   # ← added button
        btnRow.addStretch(1)
        btnRow.addWidget(openDbBtn)
        layout.addLayout(btnRow)

        # Log
        self.logView = QtWidgets.QPlainTextEdit()
        self.logView.setReadOnly(True)
        self.logView.setPlaceholderText("Logs will appear here…")
        layout.addWidget(self.logView, 2)
        self.statusBar().showMessage("Ready")

    def _apply_styles(self):
        # Prefer qdarktheme if available
        if HAS_QDARKTHEME:
            import qdarktheme  # type: ignore
            qdarktheme.setup_theme(theme="dark", corner_shape="rounded")
        else:
            app = QtWidgets.QApplication.instance()
            pal = QtGui.QPalette()
            pal.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(45, 45, 48))
            pal.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(30, 30, 30))
            pal.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(38, 38, 38))
            pal.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(230, 230, 230))
            pal.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(60, 60, 60))
            pal.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(240, 240, 240))
            pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(53, 132, 228))
            pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(255, 255, 255))
            app.setPalette(pal)
            app.setStyle("Fusion")

        self.setStyleSheet(
            """
            QPushButton { padding: 8px 14px; border-radius: 8px; }
            QLineEdit, QPlainTextEdit, QListWidget { border-radius: 8px; padding: 6px; }
            """
        )

    # -------------------- UI actions --------------------

    def browse_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select Lua files", str(self.app_dir), "Lua (*.lua)"
        )
        if files:
            self.add_files(files)

    def add_from_lineedit(self):
        t = self.pathEdit.text().strip().strip('"')
        if t:
            p = Path(t)
            if p.suffix.lower() == ".lua" and p.exists():
                self.add_files([str(p)])
            else:
                QtWidgets.QMessageBox.warning(self, "Invalid", "Please choose an existing .lua file.")
        self.pathEdit.clear()

    def add_files(self, paths: List[str]):
        added = 0
        for s in paths:
            p = Path(s)
            if p.suffix.lower() != ".lua" or not p.exists():
                continue
            if self.dropList.findItems(str(p), QtCore.Qt.MatchFlag.MatchExactly):
                continue
            self.dropList.addItem(str(p))
            added += 1
        if added:
            self.append_log(f"[ADD] {added} file(s) queued.")

    def clear_list(self):
        self.dropList.clear()
        self.append_log("[CLEAR] list cleared.")

    def set_ui_enabled(self, en: bool):
        self.injectBtn.setEnabled(en)
        self.checkBtn.setEnabled(en)
        self.locateBtn.setEnabled(en)

    def inject_clicked(self):
        files = [Path(self.dropList.item(i).text()) for i in range(self.dropList.count())]
        if not files:
            QtWidgets.QMessageBox.information(self, "No files", "Add some .lua files first.")
            return
        self.append_log("[INJECT] starting…")
        self.set_ui_enabled(False)
        w = InjectWorker(files, self.app_dir, self.bus)
        w.setAutoDelete(True)

        def re_enable():
            self.set_ui_enabled(True)

        QtCore.QTimer.singleShot(500, re_enable)
        self.thread_pool.start(w)

    def check_updates_clicked(self):
        """
        Spawn child process: python main.py --check-worker --db <path>
        """
        self.append_log("[CHECK] launching worker subprocess…")
        if self.proc and self.proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.append_log("[WARN] Worker is already running.")
            return

        db_path = self.app_dir / DB_NAME
        if not db_path.exists():
            QtWidgets.QMessageBox.warning(self, "Missing DB", f"Database not found:\n{db_path}")
            return

        self.proc = QtCore.QProcess(self)
        self.proc.setProgram(sys.executable)
        self.proc.setArguments([str(Path(__file__).resolve()), "--check-worker", "--db", str(db_path)])
        self.proc.setWorkingDirectory(str(self.app_dir))
        self.proc.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.SeparateChannels)
        self.proc.readyReadStandardError.connect(self._proc_read_stderr)
        self.proc.readyReadStandardOutput.connect(self._proc_read_stdout)
        self.proc.finished.connect(self._proc_finished)
        self._proc_stdout = b""
        self.set_ui_enabled(False)
        self.proc.start()

    def _proc_read_stderr(self):
        if not self.proc:
            return
        data = self.proc.readAllStandardError().data().decode(errors="ignore")
        if data:
            for line in data.splitlines():
                self.append_log(line)

    def _proc_read_stdout(self):
        if not self.proc:
            return
        self._proc_stdout += self.proc.readAllStandardOutput().data()

    def _proc_finished(self, code: int, status: QtCore.QProcess.ExitStatus):
        self.set_ui_enabled(True)
        if code != 0:
            self.append_log(f"[WORKER EXIT] code={code}, status={status.name}")
        raw = self._proc_stdout.decode(errors="ignore").strip()
        updates: List[dict] = []
        if raw:
            try:
                updates = json.loads(raw)
            except Exception as e:
                self.append_log(f"[PARSE ERROR] {e}\nOutput:\n{raw}")

        if not updates:
            QtWidgets.QMessageBox.information(self, "Updates", "Everything is up to date (or no entries).")
            return

        summary = "\n".join(
            f"- {u['filename']} (depot {u['depot']}): {u['current_manifest']} → {u['latest_manifest']}"
            for u in updates
        )
        r = QtWidgets.QMessageBox.question(
            self,
            "Apply updates?",
            f"The following games have updates:\n\n{summary}\n\nApply all?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )

        if r == QtWidgets.QMessageBox.StandardButton.Yes:
            self.append_log("[APPLY] updating…")
            self.set_ui_enabled(False)
            w = UpdateApplyWorker(self.app_dir, updates, self.bus)
            w.setAutoDelete(True)

            def re_enable():
                self.set_ui_enabled(True)

            QtCore.QTimer.singleShot(500, re_enable)
            self.thread_pool.start(w)
        else:
            self.append_log("[APPLY] canceled by user.")

    def locate_steam_path(self):
        """
        Finds Steam installation path and shows it; can open the folder.
        """
        try:
            path = find_steam_root()
            self.append_log(f"[STEAM] Found at: {path}")
            msg = QtWidgets.QMessageBox(self)
            msg.setIcon(QtWidgets.QMessageBox.Icon.Information)
            msg.setWindowTitle("Steam Path")
            msg.setText(f"Steam installation path:\n{path}")
            open_btn = msg.addButton("Open Folder", QtWidgets.QMessageBox.ButtonRole.ActionRole)
            copy_btn = msg.addButton("Copy", QtWidgets.QMessageBox.ButtonRole.ActionRole)
            msg.addButton(QtWidgets.QMessageBox.StandardButton.Ok)
            msg.exec()
            if msg.clickedButton() == open_btn:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))
            elif msg.clickedButton() == copy_btn:
                QtGui.QGuiApplication.clipboard().setText(str(path))
                self.append_log("[STEAM] Path copied to clipboard.")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Steam Not Found", str(e))
            self.append_log(f"[ERROR] {e}")

    def open_db_folder(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.app_dir)))

    @QtCore.pyqtSlot(str)
    def append_log(self, msg: str):
        self.logView.appendPlainText(msg)
        self.logView.verticalScrollBar().setValue(self.logView.verticalScrollBar().maximum())


# ============================ Entry points ============================

def run_gui() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app_icon = get_app_icon()
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    # Worker subprocess mode:
    if "--check-worker" in sys.argv:
        # Strip the flag before argparse in run_check_worker_cli()
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--check-worker"]
        run_check_worker_cli()
    else:
        run_gui()
