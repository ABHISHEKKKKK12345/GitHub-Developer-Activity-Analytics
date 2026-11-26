# Security and Privacy

This document describes how GitHub Developer Activity Analytics 1.0.0 handles credentials and
data, based on the source code. For reporting a vulnerability see [../SECURITY.md](../SECURITY.md).

## Summary

| Topic | Behaviour |
| --- | --- |
| Token entry | Masked entry field; held in memory for the run. **Not saved** to disk by the application. |
| Token use | `Authorization: Bearer …` header to `api.github.com`; HTTP `Authorization` header for Git operations against `https://github.com/` only. |
| Token on command lines | Not used. The Git header is supplied through environment variables of the Git child process. |
| Redaction | Token-like strings are removed from log lines, error messages, dialogs and report error text. |
| Network destinations | `api.github.com` and `github.com`. With `--self-test --online` the same two hosts. No other endpoint appears in the source. |
| Telemetry | None in the source. |
| Local data | Cloned Git history, session logs and Excel reports on the local disk. |
| Personal data | Reports contain developer names, e-mail addresses and commit messages. |

## GitHub token handling

* **Where it goes.** The token you type is read from the entry field when you press *Start*, is
  passed to the analysis thread, and is used in two ways:
  1. the `requests` session adds `Authorization: Bearer <token>` to calls to
     `https://api.github.com`;
  2. for Git, the environment variables `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0` and
     `GIT_CONFIG_VALUE_0` define `http.https://github.com/.extraheader` with an HTTP *Basic*
     header (`x-access-token:<token>`). The header applies only to `https://github.com/` and
     is **not** written into a clone's `.git/config`.
* **Limits of that design.** Environment variables of a running process can be read by software
  that runs as the same Windows user (or with administrator rights). Base64 encoding in the Basic
  header is not encryption. Use a short-lived, read-only token and revoke it afterwards.
* **No URL credentials.** URLs containing a user name or password are rejected, so a token cannot
  end up in a URL typed into the application.
* **Required permissions.** Use the least privilege that works. Public repositories need no
  token. For private repositories use a fine-grained token with read-only access (see the
  README); the application does not check scopes.
* **SAML SSO.** For organisations with SSO the token must be authorised for the organisation.

## Redaction

`redact()` removes, from any text it processes:

* the exact token string currently in use;
* GitHub token formats (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_` prefixes and `github_pat_…`);
* `Authorization:` header values and `Basic …` / `Bearer …` values;
* credentials embedded in `http(s)://user:password@…` URLs.

It is applied to the session log, on-screen log, error messages stored in the report and
messages shown in dialogs, including unexpected-exception reports. Redaction is **pattern based**:
a secret in an unusual format could pass through, so review logs before sharing them.

## Network access

* `api.github.com` — account lookup and repository listing/metadata (HTTPS, certificate
  verification enabled through `requests`).
* `github.com` — Git clone/fetch over HTTPS, using the clone URL returned by the API.
* The application does not use SSH for cloning (the SSH-style URL is only accepted as input and
  converted to an HTTPS `github.com` URL).
* With a proxy that inspects HTTPS you may see *SSL certificate* errors; install the corporate root
  certificate for Git and Windows rather than disabling verification (the application offers no
  option to disable it). For the Python (API) side, `requests` honours the `REQUESTS_CA_BUNDLE`
  environment variable, which can point to a trusted PEM-format CA bundle.

## Repository cloning and the local cache

* Repositories are cloned **without a working tree** (`--no-checkout`) and, where GitHub supports
  it, as partial clones that omit file contents. If partial clones fail, the application retries
  with a full clone, which does download file contents into the cache (still without checking them
  out). Repository files are never executed or opened by the application.
* Clones are kept under the `repositories` folder and are reused by later runs. They contain
  the **full commit history**, including author names and e-mail addresses, of every analysed
  repository — including private ones when a token is used. Protect that folder like the source
  repositories themselves and delete it when it is no longer needed.
* The application deletes only its own temporary folders (`*.cloning`, `*.old`) and the clone it
  is replacing after a failed copy; it never deletes a foreign folder that occupies a cache path.

## Logs

`logs\session_*.log` records the same messages as the on-screen log: repository names, local
paths, counts, retries and (redacted) error texts. Commit messages are not logged. Short commit
hash prefixes may appear in data-integrity notes. Logs are plain text and unencrypted.

## Generated reports

* *Commit Details* contains, per commit: author and committer **names and e-mail addresses**,
  dates, the commit hash and the **first line of the commit message**. Other sheets repeat names
  and e-mail addresses.
* Repository URLs and names (including private ones) appear in *Repository Status* and
  *Repository Summary*.
* Reports are stored unencrypted in the `output` folder.
* Under privacy law (for example GDPR) developer names and e-mail addresses are personal data.
  Decide who may receive a report, share the minimum needed, and delete reports you no longer
  need. This is not legal advice.
* The application cannot know whether commit messages or names contain confidential content.

## Recommendations

* Use a fine-grained, read-only, expiring token and revoke it when done.
* Keep `output`, `logs` and `repositories` out of version control (they are ignored by
  `.gitignore`) and off shared drives unless intended.
* Before sharing a report or log, review it and remove names, e-mail addresses, private
  repository names and message text you are not allowed to disclose.
* Never include tokens, reports or logs from private data in public issues.
