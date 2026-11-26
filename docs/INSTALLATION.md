# Installation

GitHub Developer Activity Analytics is distributed as **source code only**. You run it with
Python; there is no installer and no prebuilt executable.

## Platform support

| Platform | Status |
| --- | --- |
| Windows | Primary platform. The GUI uses Windows fonts (Segoe UI, Consolas); Windows-specific code paths exist for opening files, stopping Git process trees (`taskkill`) and the optional PortableGit folder. |
| Linux / macOS | The source contains non-Windows branches. While preparing this repository the analysis and report pipeline (URL handling, Git clone/log, identity normalisation, Excel generation) was run headlessly on Linux with Python 3.12, and `--self-test` passed except the Tkinter checks because Tkinter was not installed in that environment. The GUI itself was not run on Linux or macOS. |

## 1. Install Python

Python **3.9 or newer** is required by the source. Download it from <https://www.python.org/downloads/>.
On Windows keep **tcl/tk and IDLE** ticked in the installer: that option installs Tkinter.

Tkinter is part of Python and is **not** installed with pip. If `python -c "import tkinter"`
fails:

* Debian / Ubuntu: `sudo apt install python3-tk`
* Fedora: `sudo dnf install python3-tkinter`
* macOS: use the python.org installer, or `brew install python-tk`

## 2. Install Git

Git is required for cloning and reading commit history (version **2.31 or newer** when you use a
token).

* Windows: install *Git for Windows* from <https://git-scm.com/downloads> and make sure `git`
  is on your `PATH` (check with `git --version`).
* Linux: `sudo apt install git` (or your distribution's equivalent).
* macOS: `xcode-select --install` or `brew install git`.

Optionally, on Windows you can place a complete **PortableGit** folder named `PortableGit` next to
`GitHub_Developer_Analytics.py`; it is then preferred over Git on `PATH`. It must contain
`git.exe` and `git-remote-https.exe` in one of the architecture sub-folders (`ucrt64`,
`mingw64`, …). This is optional and not required for normal use.

## 3. Get the code and install dependencies

```powershell
git clone https://github.com/<your-account>/GitHub-Developer-Activity-Analytics.git
cd GitHub-Developer-Activity-Analytics
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

`requirements.txt` lists the only third-party runtime dependencies: `pandas`, `requests` and
`openpyxl`. Everything else (including `tkinter`) is part of the Python standard library.

## 4. Run

```powershell
python GitHub_Developer_Analytics.py
```

Check the environment without opening the GUI:

```powershell
python GitHub_Developer_Analytics.py --self-test          # offline checks
python GitHub_Developer_Analytics.py --self-test --online # adds HTTPS tests against GitHub
```

The result is written to the `logs` folder. Exit code `0` means every check passed.

## Network and disk requirements

* Outbound HTTPS access to `api.github.com` and `github.com`.
* Free disk space for the repository cache (the application refuses to clone below 300 MB free).

## Development tools (optional)

```powershell
python -m pip install -r requirements-dev.txt
ruff check .
```

## Uninstalling

Delete the project folder. Reports, logs and the clone cache are created inside it (or in
`%LOCALAPPDATA%\GitHub_Developer_Activity_Analytics` if the folder is not writable) and can be
deleted at any time.
