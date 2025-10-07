# Steamer

<p align="center">
  <img src="images/Steamer.png" alt="IshtarRF logo" width="96" />
</p>
Steamer is an open-source desktop tool for Windows that helps you:

* **Inject** game `.lua` files into Steam’s config folder in a clean, canonical format
* **Track & update** games by replacing the **ManifestID** when a newer one appears
* **Check for updates** automatically via the public data shown on **steamdb.info**

> ⚠️ **Why Steamer?**
> Many “Steam tools” circulating online are **closed-source**; community reports have raised concerns about telemetry and safety in some of them. We believe users deserve a transparent, auditable, **open-source** alternative. Steamer’s code is public so you can inspect, build, and trust what you run.

> **Educational use only.** This project is provided for learning and research. You are solely responsible for how you use it. Respect Steam/Valve terms and applicable laws. The maintainers do **not** endorse misuse.

---
## Community & Support

- 💬 **Discord:** [Join our server](https://discord.gg/kzRmTHaceS)

> Quick help, discussions, and previews happen on Discord. Come say hi!
---
## Features

* 🗂️ **Drag & Drop** or **Browse** to add `.lua` files
* 🧩 **Canonical rewrite** of each file to:

  ```lua
  addappid(APPID)
  addappid(DEPOT,1,"TOKEN")
  setManifestid(DEPOT,"MANIFEST_ID",0)
  ```
* 📦 **Safe injection** → moves the file into `<Steam>\config\stplug-in` (backs up existing files)
* 🗄️ **Local archive** → copies each injected file into a `gamesAdded/` folder
* 🧾 **SQLite logging** → `games.db` (table `gamesAdded`) tracks `filename, appid, depot, manifest_id, dest_path, moved_at`
* 🔍 **Update check** → scrapes SteamDB’s “Previously seen manifests” (headless) and compares ManifestIDs
* 🔄 **One-click apply** → updates the `.lua` file and the database to the new ManifestID
* 🖥️ **Modern dark UI** (PyQt6 + qdarktheme), no browser window shown

---

## How it works (high-level)

1. **Injection**

   * You drop a game `.lua` file into the app (or select it via *Browse → Add*).
   * Steamer reads the file (and optionally a sidecar JSON) to obtain `appid`, `depot`, `token`, `manifest_id`.
   * If any of `depot`, `token`, or `manifest_id` is missing, the file is **skipped** (no hardcoded defaults).
   * The file is rewritten to the canonical 3-line format, moved to `<Steam>\config\stplug-in`, and archived locally.
   * An entry is recorded in `games.db`.

2. **Update check**

   * For each record in `games.db`, Steamer loads the corresponding SteamDB depot page headlessly and reads the latest **ManifestID** from the “Previously seen manifests” table.
   * If a newer ManifestID is found, Steamer shows a summary and asks to **Apply**.
   * On approval, Steamer edits the target `.lua` line `setManifestid(DEPOT,"OLD",0)` → `setManifestid(DEPOT,"NEW",0)` and updates the database.

> Steamer does **not** ship with game files. You must provide your own `.lua` for each game. You do **not** need to provide a separate Manifest file—Steamer writes the ManifestID inside the `.lua`.

---

## Requirements

* **Windows 10/11**
* **Python 3.10+** (dev/build environment). Tested on 3.10–3.13.
* Google Chrome installed (undetected-chromedriver will use it)
* Network access to `steamdb.info`

Python dependencies (installed automatically during dev/build):

```
PyQt6
qdarktheme
undetected-chromedriver
selenium
beautifulsoup4
```

---

## Installing (from source)

```powershell
git clone https://github.com/SteamerLua/Steamer
cd Steamer

# Optional: use a virtual env
python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

If you don’t use `requirements.txt`, install manually:

```powershell
pip install PyQt6 qdarktheme undetected-chromedriver selenium beautifulsoup4
```

> **Note on Cloudflare**: In most regions headless access works out of the box. If SteamDB shows a browser check, open the site once in your normal Chrome to warm up a session. Advanced users can set a valid `cf_clearance` cookie in the code (when building from source).

---

## Using Steamer

1. Launch the app.
2. **Add your game `.lua` file** via drag-and-drop (the panel says *“Drag & drop .lua files here (or use Browse → Add)”*) or click **Browse** → **Add**.
3. Click **Inject / Move to Steam**.

   * Steamer auto-detects your Steam path (via registry/fallbacks), writes the canonical content, moves the file to `<Steam>\config\stplug-in`, archives a copy in `gamesAdded/`, and logs to `games.db`.
4. Click **Check Updates** anytime to compare your ManifestIDs with SteamDB.

   * If updates exist, confirm **Apply** to patch files and the database.

---

## Building a Windows EXE

We recommend **PyInstaller**:

```powershell
pip install pyinstaller
pyinstaller ^
  --name Steamer ^
  --onefile ^
  --windowed ^
  --icon steamer.ico ^
  --add-data "steamer.ico;." ^
  --collect-all selenium ^
  --collect-all undetected_chromedriver ^
  main.py
```

Artifacts will be produced under `dist\Steamer.exe`.

**Notes**

* `--windowed` hides the console.
* `--icon steamer.ico` uses your app icon (place `steamer.ico` next to `main.py`).
* `--collect-all` helps PyInstaller bundle Selenium/undetected-chromedriver resources reliably.
* First run may download a matching ChromeDriver automatically.

---

## Troubleshooting

* **“Permission denied”** when moving files
  → Run the app as **Administrator** or ensure you have write access to `<Steam>\config\stplug-in`.

* **No update results / 403** from SteamDB
  → Try again later, ensure network access, or open SteamDB once in your normal Chrome. Advanced: embed a valid `cf_clearance` in the code when building from source.

* **Nothing happens after “Check Updates”**
  → Ensure you’ve injected at least one `.lua` so there are rows in `games.db`.

* **Different Steam install path**
  → Steamer auto-detects via registry; if Steam is portable or custom, make sure the standard folders exist.

---

## Security & Privacy

* Steamer stores only the minimum needed metadata in `games.db` (`filename, appid, depot, manifest_id, dest_path, moved_at`).
* No analytics or tracking are included.
* Not affiliated with Valve/Steam/SteamDB.

---

## Credits & Origins

* **Project name**: **Steamer**

* **Maintainers**:

  * [**alhelfi**](https://github.com/alhelfi)
  * [**VergilMorvx**](https://github.com/VergilMorvx)

* **Idea & workflow (injection + updating)**: **VergilMorvx**

* **SteamDB scraper, update engine & GUI**: **alhelfi**

---

## License

This project is licensed under **AGPL-3.0**. See [`LICENSE`](LICENSE) for details.

---

## Disclaimer

This software is provided **as is**, for **educational purposes only**.
The authors and maintainers are **not responsible** for misuse, data loss, account issues, or any violations of terms of service that may result from using this tool.

---

### Why not closed-source “Steam tools”?

We prefer open-source, auditable software. Various closed-source utilities circulating under “Steam tools” names have raised community concerns about telemetry and safety. **Steamer** provides a transparent alternative you can inspect and build yourself.

