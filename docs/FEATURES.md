# Features

Every feature below is implemented in `GitHub_Developer_Analytics.py` (version 1.0.0). Anything
not listed here is not supported. Limits are described in the README under
[Limitations](../README.md#limitations-and-important-considerations).

## Inputs and scope

| Feature | Details |
| --- | --- |
| GitHub user | `https://github.com/<user>` or `https://github.com/users/<user>` |
| GitHub organisation | `https://github.com/<org>`, `https://github.com/orgs/<org>` or `…/orgs/<org>/repositories`; user vs. organisation is detected through the API |
| Single repository | `https://github.com/<owner>/<repo>`, with an optional sub-path such as `/tree/main`, with `.git`, or as `git@github.com:<owner>/<repo>.git` |
| Host restriction | Only `github.com` / `www.github.com`; URLs containing credentials are rejected |
| Reporting period | Last *N* days, 1–3650 (default 90), computed in UTC |
| Date basis | Author date (default) or committer date |

## Repository discovery (GitHub REST API)

* Account-type detection and repository listing with pagination (100 per page, safety limit of
  500 pages), sorted by `full_name` for a stable order.
* Requests carry the API version header `2022-11-28` and, if given, a `Bearer` token.
* Bounded retries with exponential back-off (2, 4, 8, 16 s) for transient errors; primary and
  secondary rate limits are honoured for up to 120 s.
* Classified errors: authentication, permission, SAML SSO, rate limit, not found, legal block,
  server and network errors, SSL errors.
* Records are validated; invalid or duplicate records are counted and reported rather than
  dropped silently.

## Repository filters

| Option | Default | Effect |
| --- | --- | --- |
| Include forks | off | Forks are listed as *Excluded* when off |
| Include archived | on | Archived repositories are listed as *Excluded* when off |
| Include disabled | off | Disabled repositories are listed as *Excluded* when off |
| Skip repos with no push in period | off | Skips repositories whose last push is more than 2 days older than the period start |
| Exclude bot accounts | off | Drops commits from bot-looking authors (name-based rule, see README) |
| Exclude merge commits | off | Drops commits with more than one parent |

A single explicitly named repository is always analysed regardless of the fork / archived /
disabled filters.

## Git history extraction

* History-only clones (`--no-checkout`) with partial-clone filters (`tree:0`, then `blob:none`,
  then a full clone as fallback); existing local copies are verified and updated with
  `git fetch --all --prune`; damaged copies are re-cloned.
* `git log --all` is streamed with bounded memory; cancellation and time-outs are checked
  every 0.5 s.
* Git is started without a shell, with prompts disabled, and with long-path support enabled.
* Retries for transient Git errors (2 retries); Git `stderr` is classified into categories with
  suggested actions.
* Free-disk-space check (300 MB) before cloning.

## Developer identity normalisation

Order of trust: GitHub noreply login → e-mail address → display name → unknown. Different
e-mail addresses are never merged automatically. Optional explicit merging through
`identity_aliases.json` (see the [example](../examples/identity_aliases.example.json)).

## Concurrency, progress and cancellation

* A background worker thread keeps the Tkinter GUI responsive; messages reach the GUI through a
  queue.
* Up to 4 repositories are processed in parallel (`MAX_PARALLEL_REPOS`, fixed in the source).
* Progress bar and live execution log (last 5,000 lines on screen) plus a session log file.
* **Cancel** stops running Git process trees; a partial report is still written.

## Excel report

* Eight worksheets: *Summary*, *Active Developers*, *Developer Summary*, *Repository Summary*,
  *Commit Details*, *Repository Status*, *Processing Errors*, *Integrity Checks*.
* Static values only (no formulas); frozen headers, filters, banded rows, landscape print layout;
  all timestamps in UTC.
* Reconciliation of commits and repositories, PASS / WARN / FAIL integrity checks.
* *Commit Details* continues on additional sheets beyond 1,000,000 rows.
* Written to a temporary file and renamed; existing reports are never overwritten; the saved file
  is re-opened and validated; a CSV fallback is written if the workbook cannot be saved.
* `_PARTIAL` / `_CANCELLED` file-name suffixes for incomplete or cancelled runs.

## Security-related behaviour

* Token entered in a masked field, never written to disk by the application.
* Token redaction in logs, messages and report error text (pattern based).
* Token passed to Git through environment variables, not on the command line.
* HTTPS certificate verification is never disabled.

## Command line

`--version`, `--self-test [--online]` and the optional developer helper `--emit-build-files`
(see the README).
