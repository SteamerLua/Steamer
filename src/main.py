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

# ============================ Icon / Path Helpers ============================

from PyQt6 import QtGui  # noqa: E402


def resource_path(rel: str) -> Path:
    """Supports normal execution and PyInstaller onefile bundles (_MEIPASS)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


def get_app_dir() -> Path:
    """Keep app data (DB/archives) next to the executable when frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


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


def copy_with_backup(src: Path, dest_dir: Path) -> Path:
    """
    Copy src into dest_dir WITHOUT modifying the original file.
    - If src is already inside dest_dir → do nothing and return the same path.
    - If target exists, rename it to *.backup / *.backup.N first, then copy.
    Returns the final target path inside dest_dir.
    """
    dest_dir = dest_dir.resolve()
    src = src.resolve()

    # Already in destination → nothing to copy/overwrite
    if src.parent.resolve() == dest_dir:
        return src

    target = dest_dir / src.name
    if target.exists():
        backup = dest_dir / f"{src.stem}.backup{src.suffix}"
        idx = 1
        while backup.exists():
            backup = dest_dir / f"{src.stem}.backup.{idx}{src.suffix}"
            idx += 1
        target.replace(backup)

    shutil.copy2(str(src), str(target))
    return target


def archive_final_copy(src_final_path: Path, archive_dir: Path) -> Path:
    """
    Copy the FINAL Lua file (as injected) into archive_dir with a timestamped name.
    """
    ensure_dir(archive_dir)
    ts = time.strftime("%Y%m%d_%H%M%S")
    archive_name = f"{src_final_path.stem}.{ts}{src_final_path.suffix}"
    archive_path = archive_dir / archive_name
    shutil.copy2(str(src_final_path), str(archive_path))
    return archive_path


# -------- Robust patterns: accept 0|1 flag and any trailing args in setManifestid ----------
RE_ADDAPPID_TOKEN = re.compile(
    r'addappid\(\s*(\d+)\s*,\s*(?:0|1)\s*,\s*"([^"]+)"\s*\)',
    re.IGNORECASE,
)
# Accepts: setManifestid(DEPOT,"MID") or setManifestid(DEPOT,"MID",0) or setManifestid(DEPOT,"MID",ANYTHING)
RE_SETMANIFEST = re.compile(
    r'setManifestid\(\s*(\d+)\s*,\s*"(\d+)"(?:\s*,\s*[^)]*)?\s*\)',
    re.IGNORECASE,
)
RE_ADDAPPID_SINGLE = re.compile(r'addappid\(\s*(\d+)\s*\)(?!\s*,)', re.IGNORECASE)


def infer_appid_from_filename(path: Path) -> int:
    m = re.search(r"\d+", path.stem)
    if not m:
        raise ValueError(f"Cannot infer APPID from filename: {path.name}")
    return int(m.group(0))


def parse_all_from_content(text: str) -> Dict[str, object]:
    """
    Extract:
      - appid from addappid(APPID)
      - tokens map: depot -> token from addappid(DEPOT, 0|1, "TOKEN")
      - manifests map: depot -> manifest_id from setManifestid(DEPOT, "MID", ANY)
    """
    out: Dict[str, object] = {"appid": None, "tokens": {}, "manifests": {}}

    m = RE_ADDAPPID_SINGLE.search(text)
    if m:
        out["appid"] = int(m.group(1))

    tokens = {int(d): t for d, t in RE_ADDAPPID_TOKEN.findall(text)}
    manifests = {int(d): mid for d, mid in RE_SETMANIFEST.findall(text)}

    out["tokens"] = tokens
    out["manifests"] = manifests
    return out


def load_sidecar_json(stem: str, folder: Path) -> Dict[str, object]:

    cfg = folder / f"{stem}.json"
    result: Dict[str, object] = {"appid": None}
    if not cfg.exists():
        return result
    try:
        with cfg.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "appid" in data:
            result["appid"] = int(data["appid"])
    except Exception:
        pass
    return result


def build_lua_content_multi(appid: int, tokens: Dict[int, str], manifests: Dict[int, str]) -> str:
    lines: List[str] = [f"addappid({appid})"]
    all_depots = sorted(set(tokens.keys()) | set(manifests.keys()))
    for d in all_depots:
        tok = tokens.get(d)
        if tok:
            lines.append(f'addappid({d},1,"{tok}")')
    for d in all_depots:
        mid = manifests.get(d)
        if mid:
            lines.append(f'setManifestid({d},"{mid}",0)')
    return "\n".join(lines) + "\n"


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
    # Migration: add 'multi' column if missing (0/1)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(gamesAdded)").fetchall()}
        if "multi" not in cols:
            conn.execute("ALTER TABLE gamesAdded ADD COLUMN multi INTEGER DEFAULT 0;")
            conn.commit()
    except Exception:
        pass
    conn.commit()
    return conn


def record_in_db(
    conn: sqlite3.Connection,
    filename: str,
    appid: int,
    depot: int,
    manifest_id: str,
    dest_path: Path,
    multi: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO gamesAdded (filename, appid, depot, manifest_id, dest_path, multi) VALUES (?, ?, ?, ?, ?, ?);",
        (filename, appid, depot, manifest_id, str(dest_path), int(multi)),
    )
    conn.commit()


def load_latest_rows(conn: sqlite3.Connection) -> List[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT filename, appid, depot, manifest_id, dest_path, moved_at, multi
        FROM gamesAdded
        ORDER BY moved_at ASC
        """
    )
    rows = cur.fetchall()
    latest: Dict[Tuple[str, int], dict] = {}
    for filename, appid, depot, manifest_id, dest_path, moved_at, multi in rows:
        latest[(filename, int(depot))] = {
            "filename": filename,
            "appid": int(appid),
            "depot": int(depot),
            "manifest_id": str(manifest_id),
            "dest_path": str(dest_path),
            "moved_at": str(moved_at),
            "multi": int(multi) if multi is not None else 0,
        }
    return list(latest.values())


def load_known_files(conn: sqlite3.Connection) -> List[dict]:
    """
    Unique files we know about from DB (for worker to parse all depots directly from file).
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT filename, dest_path
        FROM gamesAdded
        ORDER BY moved_at DESC
        """
    )
    return [{"filename": fn, "dest_path": dp} for (fn, dp) in cur.fetchall()]


def update_db_manifest(conn: sqlite3.Connection, filename: str, depot: int, new_manifest: str) -> int:
    cur = conn.cursor()
    cur.execute(
        "UPDATE gamesAdded SET manifest_id = ? WHERE filename = ? AND depot = ?",
        (new_manifest, filename, depot),
    )
    conn.commit()
    return cur.rowcount


def update_lua_manifest(file_path: Path, depot: int, new_manifest: str) -> bool:
    """
    Replace setManifestid(<depot>,"OLD",ANY) with setManifestid(<depot>,"NEW",0)
    """
    if not file_path.exists():
        return False
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False

    pattern = re.compile(
        rf'(?im)^(\s*setManifestid\(\s*{depot}\s*,\s*")(\d+)(".*\)\s*)$'
    )

    def repl(m):
        return f'{m.group(1)}{new_manifest}",0)'

    new_text, n = pattern.subn(repl, text)
    if n == 0:
        pattern2 = re.compile(
            rf'(setManifestid\(\s*{depot}\s*,\s*")(\d+)(".*\))',
            re.IGNORECASE,
        )
        new_text, n = pattern2.subn(lambda m: f'{m.group(1)}{new_manifest}",0)', text, count=1)
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

    # Use unique file list; then parse ALL depots from each file itself
    files = load_known_files(conn)
    if not files:
        print("[INFO] No known files in DB (gamesAdded is empty).", file=sys.stderr, flush=True)
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

        for file_row in files:
            filename = file_row["filename"]
            dest_path = Path(file_row["dest_path"])
            lua_file = dest_path / filename
            if not lua_file.exists():
                print(f"[MISSING] {lua_file}", file=sys.stderr, flush=True)
                continue

            try:
                text = lua_file.read_text(encoding="utf-8", errors="ignore")
            except Exception as e:
                print(f"[READ ERROR] {lua_file}: {e}", file=sys.stderr, flush=True)
                continue

            parsed = parse_all_from_content(text)
            appid = int(parsed.get("appid") or 0)
            manifests: Dict[int, str] = parsed.get("manifests", {})  # type: ignore[assignment]
            if not manifests:
                print(f"[NO MANIFESTS] {filename}", file=sys.stderr, flush=True)
                continue

            multi = 1 if len(manifests) > 1 else 0

            for depot, current_manifest in sorted(manifests.items()):
                checked += 1
                try:
                    latest_manifest = get_latest_manifest_for_depot(driver, depot)
                except Exception as e:
                    print(f"[CHECK ERROR] {filename} depot {depot}: {e}", file=sys.stderr, flush=True)
                    errors += 1
                    continue

                if not latest_manifest:
                    print(f"[NO DATA] depot {depot} ({filename})", file=sys.stderr, flush=True)
                    continue

                if latest_manifest != current_manifest:
                    to_update.append(
                        {
                            "filename": filename,
                            "appid": appid,
                            "multi": multi,
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
    Background worker to copy .lua files into Steam and log to DB WITHOUT modifying the content.
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
        copied = skipped = errors = archived = 0

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

                parsed = parse_all_from_content(original_text)
                sidecar = load_sidecar_json(src.stem, src.parent)

                # appid (for DB only)
                appid = parsed.get("appid") or sidecar.get("appid")
                if not appid:
                    try:
                        appid = infer_appid_from_filename(src)
                    except Exception:
                        appid = 0
                appid = int(appid or 0)

                manifests: Dict[int, str] = dict(parsed.get("manifests", {}))  # type: ignore[assignment]

                # Copy file to Steam (no edits)
                final_path = copy_with_backup(src, dest_dir)

                # Archive the injected copy
                archived_path = archive_final_copy(final_path, archive_dir)
                self.log.message.emit(f"[ARCHIVE] {archived_path.name}")

                if manifests:
                    multi_flag = 1 if len(manifests) > 1 else 0
                    for d, mid in sorted(manifests.items()):
                        record_in_db(conn, final_path.name, int(appid), int(d), str(mid), dest_dir, multi=multi_flag)
                        self.log.message.emit(f"[INJECTED] {final_path.name} -> depot {d} (manifest {mid})")
                else:
                    self.log.message.emit(
                        f"[COPY] {final_path.name} copied (no setManifestid found — updater can’t track this file)."
                    )

                copied += 1
                archived += 1

            except PermissionError:
                self.log.message.emit(f"[ERROR] Permission denied: {src.name} (run as Administrator).")
                errors += 1
            except Exception as ex:
                self.log.message.emit(f"[ERROR] {src.name}: {ex}")
                errors += 1

        conn.close()
        self.log.message.emit(
            f"\n[INJECT DONE] copied={copied}, archived={archived}, skipped={skipped}, errors={errors}"
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
            appid = int(item.get("appid", 0))
            multi = int(item.get("multi", 0))
            depot = int(item["depot"])
            new_manifest = str(item["latest_manifest"])
            dest_path = Path(item["dest_path"])
            lua_path = Path(item["lua_path"])

            ok_file = update_lua_manifest(lua_path, depot, new_manifest)
            ok_db = False
            if ok_file:
                try:
                    affected = update_db_manifest(conn, filename, depot, new_manifest)
                    if affected == 0:
                        record_in_db(conn, filename, appid, depot, new_manifest, dest_path, multi=multi)
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

        # Keep app data next to EXE, not in _MEI temp
        self.app_dir = get_app_dir()

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

        # Find Steam Path button
        self.locateBtn = QtWidgets.QPushButton("Find Steam Path")
        self.locateBtn.setIcon(self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DirIcon))
        self.locateBtn.clicked.connect(self.locate_steam_path)

        openDbBtn = QtWidgets.QPushButton("Open DB Folder")
        openDbBtn.clicked.connect(self.open_db_folder)

        btnRow.addWidget(self.injectBtn)
        btnRow.addWidget(self.checkBtn)
        btnRow.addWidget(self.locateBtn)
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
        Start same EXE with worker flag:
          - Frozen (exe): <exe> --check-worker --db <path>
          - Source:       python main.py --check-worker --db <path>
        """
        self.append_log("[CHECK] launching worker subprocess…")
        if self.proc and self.proc.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.append_log("[WARN] Worker is already running.")
            return

        db_path = self.app_dir / DB_NAME

        # Ensure DB exists (create schema if missing)
        if not db_path.exists():
            try:
                conn = open_db(db_path)
                conn.close()
                self.append_log(f"[INFO] Created new DB at {db_path}")
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "DB Error", f"Could not create DB:\n{e}")
                return

        self.proc = QtCore.QProcess(self)

        if getattr(sys, "frozen", False):
            program = sys.executable
            args = ["--check-worker", "--db", str(db_path)]
        else:
            program = sys.executable
            script = Path(__file__).resolve()
            args = [str(script), "--check-worker", "--db", str(db_path)]

        self.proc.setProgram(program)
        self.proc.setArguments(args)
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
