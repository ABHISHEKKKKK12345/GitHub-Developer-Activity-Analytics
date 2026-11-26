# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.0   | Yes       |

Only the latest release receives fixes.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

1. Use GitHub's **private vulnerability reporting**: open the repository's **Security** tab and
   choose **Report a vulnerability**.
2. If that option is not available, contact the maintainer, Abhishek Srivastava, with a private
   message on [LinkedIn](https://www.linkedin.com/in/abhishek-srivastava-1538461b1/) and ask for a
   private channel. Do not put technical details in the first message.

Please include the application version, your operating system and Git version, steps to
reproduce, and the impact you expect. **Never include a real GitHub token, a generated report,
or log files without checking them first** — see below.

Reports are handled on a best-effort basis by a single maintainer; no response-time guarantee
is offered. Please allow a reasonable period for a fix before any public disclosure (coordinated
disclosure).

## Protecting your GitHub token

* Use the **least privilege** that works: a fine-grained token limited to the repositories or
  organisation you need, with read-only repository access (see the README for details). Public
  repositories need no token.
* Give the token an expiry date and revoke it when you no longer need it.
* Never commit a token, paste it into an issue, or send it to anyone. The application does not
  save the token to disk; it exists only in the entry field and in memory while the program runs.
* The application removes token-like strings from its logs, error messages and reports, but this
  is pattern-based redaction. Review logs and reports before sharing them.

## What to review before sharing logs or reports

Generated workbooks contain developer names, e-mail addresses and commit messages. Logs contain
repository names, local paths and error text. Both can be sensitive. See
[docs/SECURITY_AND_PRIVACY.md](docs/SECURITY_AND_PRIVACY.md).
