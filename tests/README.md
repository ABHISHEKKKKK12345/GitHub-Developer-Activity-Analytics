# Tests

This repository does **not** contain an automated test suite yet, and no test results are
claimed anywhere in the documentation.

What exists today:

* A runtime environment check built into the application:
  `python GitHub_Developer_Analytics.py --self-test [--online]`. It checks imports, writable
  folders, certificates, Tkinter, Excel writing and Git, but it does not test the analysis logic.
* Static checks run by `.github/workflows/python-checks.yml`: syntax compilation, `ruff`, JSON
  validation of the example file and an import smoke test.

Contributions that add a `pytest` suite are welcome, for example for `normalise_identity()`,
`is_bot()`, `GitHubAnalyzer.parse_github_url()` and report generation against a small synthetic
local Git repository (no network or real developer data required). Use fictional data only.
