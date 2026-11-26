# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Repository prepared for publication as a **source-code-only** project: documentation,
  configuration, issue/pull-request templates and a GitHub Actions workflow were added or revised;
  the executable-build guide was removed.
- Source: the module docstring now carries the author and project name and documents the
  command-line options with the correct file name; a comment explains HTTPS certificate
  verification (`REQUESTS_CA_BUNDLE`); an unused `import math` was removed; the file now ends
  with a newline. No behaviour changed.

## [1.0.0] - initial release (release date to be set when the version is tagged)

First release of **GitHub Developer Activity Analytics**. Everything below is implemented in
`GitHub_Developer_Analytics.py`.

### Added

- Tkinter desktop GUI with source URL, reporting period (1–3650 days), date basis (author or
  committer date), optional GitHub token, six repository/commit filters, progress bar,
  execution log, and *Start*, *Cancel*, *Open report*, *Open output folder* and *Clear log* buttons.
- Input handling for GitHub user, organisation, `orgs/<name>/repositories`, repository,
  repository sub-path (for example `/tree/main`), `.git` and `git@github.com:` SSH-style URLs.
- GitHub REST API integration (API version header `2022-11-28`) for account-type detection,
  repository discovery with pagination, and repository metadata, including bounded retries with
  exponential back-off and handling of primary and secondary rate limits.
- Git-based history extraction: history-only clones (`--no-checkout`, partial-clone filters with
  automatic fallback), fetch/prune for existing local copies, automatic re-clone of damaged copies,
  and streaming read of `git log --all` with bounded memory.
- Parallel repository processing (up to 4 repositories at a time) with responsive cancellation of
  running Git processes.
- Developer identity normalisation (GitHub noreply login → e-mail address → display name) and
  explicit merging through an optional `identity_aliases.json` file.
- Filters: include forks, include archived, include disabled, exclude bot accounts, exclude merge
  commits, skip repositories without a push in the period.
- Excel report (`.xlsx`) with eight worksheets: *Summary*, *Active Developers*,
  *Developer Summary*, *Repository Summary*, *Commit Details*, *Repository Status*,
  *Processing Errors* and *Integrity Checks*; automatic splitting of *Commit Details* beyond
  1,000,000 rows per sheet; CSV fallback if the workbook cannot be written.
- Reconciliation and integrity checks, and a post-save validation of the workbook.
- Partial reports for cancelled or incomplete runs (`_CANCELLED` / `_PARTIAL` file-name suffix).
- Security: token redaction in logs, errors and reports; token passed to Git through the
  environment rather than the command line.
- Runtime support in the source: PortableGit detection (beside the script) with system-Git
  fallback, `--self-test [--online]`, and an optional `--emit-build-files` developer helper that
  writes PyInstaller build files. No executable is distributed.
