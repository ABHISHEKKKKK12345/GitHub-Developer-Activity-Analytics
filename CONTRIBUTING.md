# Contributing

Thank you for your interest in **GitHub Developer Activity Analytics**. Contributions of all
sizes are welcome: bug reports, documentation fixes, ideas and code.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting bugs

Open an issue and include:

* application version (`python GitHub_Developer_Analytics.py --version`), operating system and
  version, Python version and Git version (`git --version`);
* what you entered (a **public** example URL if possible), the settings and filters used;
* what you expected and what happened, plus the relevant lines of the `session_*.log` file.

**Remove tokens, private repository names, developer names and e-mail addresses** from anything
you attach. Never attach a generated report from private data. Security problems must be reported
privately — see [SECURITY.md](SECURITY.md).

## Requesting features

Open an issue that describes the problem you want to solve and, if you can, the expected
behaviour and an example. Please check the *Limitations* section of the README first; some
behaviour (for example fixed parallelism of 4 repositories) is deliberate in version 1.0.0.

## Development setup

```powershell
git clone https://github.com/<your-account>/GitHub-Developer-Activity-Analytics.git
cd GitHub-Developer-Activity-Analytics
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python GitHub_Developer_Analytics.py --self-test
```

`requirements-dev.txt` contains the runtime dependencies plus `ruff` (static analysis).
Replace `<your-account>` with the account of your fork.

The same checks run in GitHub Actions (`.github/workflows/python-checks.yml`): Python syntax
check, `ruff check .`, validation of the example JSON file and an import smoke test.

## Code quality expectations

* The application is intentionally a **single file**, `GitHub_Developer_Analytics.py`, with no
  GUI code in the analysis classes. Keep it that way unless a change is discussed first.
* Run the linter before opening a pull request: `ruff check .` (configuration in
  `pyproject.toml`). New code should not add findings.
* Follow `.editorconfig`. The source file uses CRLF line endings and is marked `-text` in
  `.gitattributes`; do not convert line endings in an unrelated change.
* Never print, log or store secrets; pass any text that may contain a token through `redact()`.
* Keep the GUI responsive: long work belongs in the worker thread; the GUI thread only handles
  messages from the queue.
* Preserve report structure (worksheet and column names) unless the change is intentional, and
  then update [docs/USER_GUIDE.md](docs/USER_GUIDE.md), the README and the changelog.
* **There is no automated test suite yet.** If you add logic, please describe in the pull request
  how you verified it (for example against a small public repository). A proposal to introduce a
  test framework is welcome as its own pull request.
* Use only fictional data (`developer_alpha`, `developer@example.invalid`, `example-organization`,
  `sample-repository`) in documentation, examples and issues.

## Pull request process

1. Fork the repository and create a branch from the default branch (`feature/short-description`
   or `fix/short-description`).
2. Make focused changes; one topic per pull request.
3. Run `ruff check .`, `python -m py_compile GitHub_Developer_Analytics.py` and
   `python GitHub_Developer_Analytics.py --self-test`.
4. Update documentation and `CHANGELOG.md` (under an *Unreleased* heading) when behaviour changes.
5. Open the pull request, describe **what** and **why**, how you tested it, and link related issues.
6. Respond to review comments. The maintainer merges when the change is ready; reviews are done on
   a best-effort basis.

By contributing you agree that your contribution is licensed under the project's license
(see [LICENSE](LICENSE)).
