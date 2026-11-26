# User Guide

This guide explains how to run **GitHub Developer Activity Analytics** v1.0.0 and how to read
the Excel report it produces. All names, e-mail addresses and numbers in examples are fictional.

## Contents

1. [Before you start](#before-you-start)
2. [Running an analysis](#running-an-analysis)
3. [Choosing the settings](#choosing-the-settings)
4. [Authentication](#authentication)
5. [While the analysis runs](#while-the-analysis-runs)
6. [When the analysis ends](#when-the-analysis-ends)
7. [Excel report reference](#excel-report-reference)
8. [Reading the numbers](#reading-the-numbers)
9. [Re-running and the local cache](#re-running-and-the-local-cache)
10. [Merging identities](#merging-identities)
11. [Troubleshooting](#troubleshooting)

## Before you start

* Decide **what** to analyse: a user (`https://github.com/developer_alpha`), an organisation
  (`https://github.com/example-organization`) or one repository
  (`https://github.com/example-organization/sample-repository`).
* Decide **which period** (in days) and whether the **author date** or the **committer date**
  should count.
* For private repositories, or large organisations, create a [token](#authentication).
* Make sure Git is installed and on your `PATH` (see [INSTALLATION.md](INSTALLATION.md)).
* Start with a **small** target (one repository) to get a feeling for run time and output.

## Running an analysis

1. Start the application: `python GitHub_Developer_Analytics.py`.
2. Paste the URL into **1. GitHub Source**. Supported spellings are listed in the
   [README](../README.md#supported-github-url-formats).
3. Set **Reporting period (days)** (1–3650) and **Date basis** in **2. Analysis Settings**.
4. Optionally paste the token.
5. Tick or clear the filters in **3. Repository & Commit Filters**.
6. Click **START ANALYSIS**. Invalid URLs, an invalid period, or missing Git are reported
   immediately in a message box and nothing starts.
7. Watch the progress bar and log. The first phase (discovering repositories) shows an animated
   bar; afterwards it shows the percentage of selected repositories processed.
8. Read the completion dialog and open the report.

## Choosing the settings

### Reporting period

The window starts *N* days before the moment the run started and ends at that moment (UTC).
Commits dated outside the window — including commits dated in the future — are not counted; they
appear in the *Excluded: Outside Period* column of *Repository Status*.

### Date basis

| Choice | Meaning | When to use |
| --- | --- | --- |
| **Author date** (default) | When the change was *written*. | Measuring when developers did their work. |
| **Committer date** | When the commit was *applied* (merged, rebased, cherry-picked, amended). | Measuring when changes entered the history. |

Both dates are stored for every commit in *Commit Details*; the one in use is shown in
**Date Used (UTC)**. The choice affects the period filter and every statistic and also appears in
the file name (`…_author_…` or `…_committer_…`).

### Filters

| Filter | Default | Effect |
| --- | --- | --- |
| Include forks | off | Forks often carry upstream authors and history; including them can inflate developer counts. |
| Include archived | **on** | Archived repositories are read-only but may still contain commits in the period. |
| Include disabled | off | Disabled repositories normally cannot be cloned; if included they are listed as *Failed*. |
| Exclude bot accounts | off | Removes commits whose author looks like a bot (name-based, see README). The number removed is shown per repository and in the Summary. |
| Exclude merge commits | off | Removes commits with more than one parent. |
| Skip repos with no push in period | off | Saves time on large organisations by not cloning repositories whose last push is clearly older than the window. The result can differ slightly from a full run in edge cases because it relies on GitHub's push date. |

Repositories removed by a repository filter are **not hidden**: they remain in *Repository Status*
with status *Excluded* and the reason. For a **single-repository URL**, these three repository
filters are not applied.

## Authentication

* **Public repositories:** no token needed, but the API allows only 60 requests per hour without
  one. A token raises that to 5,000 per hour.
* **Private repositories:** a token is required, and it must be allowed to read those
  repositories. See the README section [GitHub token setup](../README.md#github-token-setup).
* To include your own private repositories, analyse **your own user URL with your own token**.
* The token is never saved by the application. You must paste it again at the next start.

## While the analysis runs

* Up to **four repositories** are processed at the same time; their log lines are prefixed with
  `[owner/repository]`.
* Network and Git problems that look temporary are retried automatically.
* If the GitHub rate limit is hit and the reset is less than two minutes away, the application waits
  and continues.
* **CANCEL ANALYSIS** stops running Git processes promptly (cancellation is checked every half second), marks unfinished
  repositories as *Cancelled / Not Processed* and still writes a report named `…_CANCELLED.xlsx`.
  Cancelling during the very first (discovery) phase writes no report.
* Closing the window during a run asks for confirmation; no report is written in that case.

## When the analysis ends

A dialog shows the outcome, the reconciliation of repositories, developers, commits, the activity
span and the report path. The same outcome is shown in colour at the top of the *Summary* sheet.

| Outcome | Colour | Meaning |
| --- | --- | --- |
| Fully successful analysis | green | Everything selected was analysed, discovery was complete and no integrity check failed. |
| Analysis with skipped repositories | amber | Some repositories were skipped (empty, or no push in the period with *skip* on). |
| Completed with integrity-check failures | amber | Numbers did not reconcile — see *Integrity Checks*. |
| No repositories were analysed | amber | Nothing qualified (for example all repositories were filtered out). |
| Partially successful analysis (some repositories failed) | red | At least one repository failed — see *Repository Status* and *Processing Errors*. |
| Incomplete repository discovery (partial report) | red | The repository list could not be retrieved completely. |
| Cancelled analysis (partial report) | red | The run was cancelled. |

Any outcome other than *Fully successful analysis* adds `_PARTIAL` (or `_CANCELLED`) to the file
name. After saving, the application re-opens the workbook and cross-checks sheet totals; any
problem is shown in the dialog and the log.

## Excel report reference

The workbook has eight worksheets. Timestamps are UTC (`yyyy-mm-dd hh:mm:ss`). The workbook
contains no formulas. Text that begins with `=` (for example in a commit message) is stored as
text, never as a formula.

### 1. Summary

Colour-coded, read-only overview. Sections, in order:

| Section | Contents |
| --- | --- |
| *Run outcome* | The outcome label (see above). |
| **Headline KPIs** | Unique active developers, total commits, repositories analysed *x* of *y*, days with commits *x* of *y*. |
| **1. Report details** | Report title/version, source URL, account type (*User*, *Organization* or *Repository (direct URL)*), generation time. |
| **2. Reporting period** | Period length, start and end (UTC), time zone, date basis. |
| **3. Repository reconciliation** | Discovered, selected, analysed, excluded, skipped, failed, cancelled; the reconciliation sentence; whether discovery completed. |
| **4. Commit & developer results** | Total commits, unique developers, developer–repository pairs, repositories with commits, first/latest commit, activity span, active days, inactive days. |
| **5. Filters applied** | State of each filter, how many repositories/commits each removed, commits outside the period. |
| **6. Run notes & limitations** | Warnings, integrity-check tally, history scope, identity and counting notes. |
| **7. Active developers – complete list** | Developer, total commits and most frequent e-mail of every active developer. |

### 2. Active Developers

One row per identity, sorted by commits (highest first), then name.

| Column | Meaning |
| --- | --- |
| Developer Name | Most frequent author name of the identity. |
| Developer Email | Most frequent author e-mail of the identity. |
| Total Commits | Qualifying commits. Data bars show relative size. |
| Repository Count | Repositories with at least one qualifying commit by this identity. |
| Repository Names | Those repositories (`owner/name`), alphabetically, comma separated. |
| First / Latest Commit Date (UTC) | Earliest / latest *Date Used*. |
| Commit Activity Duration (Days) | Inclusive calendar days from first to latest commit date. |
| Active Days | Distinct UTC dates with a commit. |
| Share of Total Commits (%) | Percentage of all qualifying commits. |

The TOTAL row shows the number of unique developers, the sum of commits and of repository
counts (pairs), the overall first/latest commit, and the overall span and active days.

### 3. Developer Summary

The same developers with more detail.

| Column | Meaning |
| --- | --- |
| Rank | 1 = most commits. |
| Developer Name, Developer Email | As above. |
| Normalised Developer Identity | The identity key (`github:…`, `email:…`, `name:…`, `unknown:…` or an alias target). |
| Identity Method | How the key was derived (most frequent method among the identity's commits). |
| Total Commits, Share of Total Commits (%) | As above. |
| Repository Count, Repository Names | As above. |
| First / Latest Commit Date (UTC), Commit Activity Duration (Days), Active Days | As above. |
| Avg Commits per Active Day | Total commits ÷ active days, two decimals. |
| Name Variants | All author names seen for the identity, most frequent first, separated by `;`. |
| Email Variants | All author e-mails seen for the identity, separated by `;`. |

### 4. Repository Summary

One row per **analysed** repository (including analysed repositories with no qualifying commits,
which show 0 and empty dates).

| Column | Meaning |
| --- | --- |
| Repository Name, Repository Full Name | Name and `owner/name`. |
| Total Commits | Qualifying commits in the repository. |
| Unique Developers | Distinct identities with a commit in it. |
| First / Latest Commit Date (UTC) | Earliest / latest *Date Used* in the repository. |
| Commit Activity Duration (Days), Active Days | As above, per repository. |
| Share of Total Commits (%) | Repository's part of all qualifying commits. |

Sorted by commits, then full name. In the TOTAL row, *Unique Developers* is the sum of the
column (developer–repository pairs), while the Summary sheet gives the true number of distinct
developers.

### 5. Commit Details

One row per qualifying commit; sorted newest first by *Date Used*. This is the evidence behind all
other sheets.

| Column | Meaning |
| --- | --- |
| Repository Name, Repository Full Name | Where the commit was found. |
| Developer Name, Developer Email | Commit **author**. |
| Author Date (UTC) | When the author wrote the commit. |
| Committer Name, Committer Email, Committer Date (UTC) | Who applied it and when. |
| Date Used (UTC) | The date used for filtering and statistics (author or committer date). |
| Commit Hash | Full commit id. |
| Commit Message | First line (subject) only. |
| Normalised Developer Identity, Identity Method | Identity key and how it was derived. |

### 6. Repository Status

One row per **discovered** repository, whatever happened to it.

| Column | Meaning |
| --- | --- |
| Repository Name, Repository Full Name, Repository URL | Identification. |
| Visibility | `public`, `private` or `internal` as reported by GitHub (`unknown` for an unreadable API record). |
| Fork Status, Archived Status, Disabled Status | `Yes` / `No` from GitHub. |
| Last Push (UTC) | GitHub's last-push timestamp. |
| Commits Read (all history) | Commit records returned by Git, before filtering. |
| Commits Found | Qualifying commits kept. |
| Excluded: Bots / Merges / Outside Period / Invalid/Duplicate Records | Commits dropped, by reason. |
| Processing Status | *Analysed*, *Excluded*, *Skipped*, *Failed*, *Cancelled / Not Processed*. |
| Reason | Why a repository was excluded, skipped or failed; a note if analysed with no commits. |

Ledger rule: *Commits Read* = *Commits Found* + the four *Excluded* columns. Commit records that
Git returned but that could not be parsed are counted under *Invalid/Duplicate Records*.

### 7. Processing Errors

| Column | Meaning |
| --- | --- |
| Timestamp (UTC), Repository | When and where. |
| Processing Stage | For example *GitHub API*, *Repository discovery*, *Clone/Fetch*, *Reading history*, *Commit parsing*. |
| Error Category | For example *Network*, *Authentication*, *Rate limit (primary)*, *Disk space*, *Data integrity*. |
| Error Message | Redacted message. |
| Retry Count | Retries performed. |
| Final Status | *Failed*, *Recorded (not fatal)*, *Discovery incomplete*. |
| Recommended Corrective Action | Suggested fix. |

If nothing went wrong, a single row states *"No processing errors occurred."* *Recorded (not
fatal)* rows describe data notes, such as commits without author information (kept as
*unknown*) or unparsable commit records (excluded).

### 8. Integrity Checks

Columns: *Check*, *Expected*, *Actual*, *Result* (**PASS**, **WARN**, **FAIL**) and *Detail*.
The standard checks are:

1. Sum of developer commit counts = total commits
2. Sum of repository commit counts = total commits
3. Commit Details rows = total commits
4. Repository Status "Commits Found" = total commits
5. Active Developers rows = unique developers
6. Repository reconciliation (discovered = analysed + excluded + skipped + failed + cancelled)
7. Every discovered repository has exactly one status row
8. Repository Summary rows = analysed repositories
9. Commit ledger: commits read = qualifying + every excluded bucket
10. Active days + inactive days = activity span

When there is at least one commit, three more are added: all commit dates fall inside the
reporting period; no duplicate commit within a repository; and — as a **WARN**, not a failure —
commit hashes shared by several repositories (forks or mirrors). Discovery or run warnings are
appended as additional **WARN** rows.

## Reading the numbers

* **Unique active developers** counts identities. Check *Developer Summary → Name Variants* and
  *Email Variants* for people who appear twice, and consider [aliases](#merging-identities).
* **Bots** are included unless you tick *Exclude bot accounts*. Automation accounts can be the
  largest "developers" in a repository.
* **Commit Activity Duration (Days)** is a span, **Active Days** is the number of days with
  commits, and *Avg Commits per Active Day* divides commits by active days. None of them measures
  effort.
* The same commit can appear in several repositories (forks); the sum over repositories can then
  exceed the number of distinct commits.
* A repository with *0* commits is simply quiet in the period (or filtered by bot/merge options);
  check *Excluded: …* in *Repository Status*.

## Re-running and the local cache

Cloned histories are kept in the `repositories` folder. The next run fetches updates
(`git fetch --all --prune`) instead of cloning again, which is much faster. The cache is never
cleaned automatically. Reports are never overwritten: if a name is taken, `_2`, `_3`, … is appended.

## Merging identities

1. Run an analysis and open *Developer Summary*.
2. Copy the *Normalised Developer Identity* values of the entries that are the same person.
3. Create `identity_aliases.json` next to the script based on
   [`identity_aliases.example.json`](../examples/identity_aliases.example.json): each **key** is a value to
   replace, each **value** is the identity to use instead.
4. Run again. Merged rows show the method *Alias mapping*.

Keys and values are compared in lower case. A file that cannot be read produces a warning and is
ignored for that run.

## Troubleshooting

See the table in the README: [Troubleshooting](../README.md#troubleshooting). The newest
`session_*.log` in the `logs` folder contains the full (redacted) details of every run.
