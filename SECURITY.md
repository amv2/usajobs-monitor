# Security policy

usajobs-monitor handles your USAJOBS API key, SMTP passwords, webhook URLs and, with the AI add-on, your resume. If you find a way it could leak or misuse any of them, please tell me privately.

## Reporting a vulnerability

Use GitHub's private reporting: the repository's **Security** tab, then **Report a vulnerability** ([direct link](https://github.com/amv2/usajobs-monitor/security/advisories/new)). Please don't open a public issue, pull request or discussion for a security problem.

Include what you can of:

- the version (`usajobs-monitor --version`) and how you installed it (pip, pipx, Docker, GitHub Actions template)
- the steps or config that show the problem, with your real credentials, profile, decisions file and reports removed
- what an attacker could do with it

This is a one-person, spare-time project, so responses are best effort. I aim to acknowledge a report within a week and will keep you updated in the advisory until it's fixed or closed. Fixes ship as a new release on PyPI and GitHub, and you're credited in the advisory unless you'd rather not be.

## Supported versions

Only the latest release gets security fixes. Upgrade with `pipx upgrade usajobs-monitor` (or your installer's equivalent), or pull the latest `main` if you run from a clone, Docker build or Actions template copy.

## In scope

- Credentials, webhook URLs or resume text reaching anything other than the service they're meant for (logs, reports, failure notices, the AI provider when `send_resume` is off, a redirect target)
- Reports, history or decisions written outside the configured folders
- The GitHub Actions workflow, Dockerfile or release workflow exposing secrets or allowing untrusted code to run
- Dependencies with known vulnerabilities that affect this tool

## Out of scope

- Problems in USAJOBS itself, your mail server, Discord, Slack, ntfy or the AI provider. Report those to their owners.
- Secrets you committed to your own repository. See [Keeping secrets out of the repo](README.md#keeping-secrets-out-of-the-repo) for how to rotate and clean them up.
