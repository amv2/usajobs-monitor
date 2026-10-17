# usajobs-monitor

A scheduled job search for [USAJOBS](https://www.usajobs.gov) that does the filtering a keyword search can't. You describe the jobs you want and the hiring paths you qualify for in one config file, and each run gives you a short list of postings worth reading, a watch list, and the near misses, with a reason for every job it set aside.

> **Status:** early. The core search, filtering, tracking and reports (phase 1) are ported from a personal tool that runs three times a week, and delivery by email, Discord, Slack and ntfy is in. Features marked *(planned)* aren't in the package yet. See [Roadmap](#roadmap).

## What it does

- **Searches USAJOBS through the official API.** It runs every search in your profile, reads every page of results (500 per page, up to 10 pages), and warns you when a search was cut off.
- **Filters out what boilerplate hides.** Keyword searches return lots of noise. This tool checks the structured fields instead:
  - temporary and term jobs, from the appointment-type code rather than the title
  - senior executive roles, from the pay plan (SES and equivalents, plus any pay plans you list)
  - wrong occupations, from whole words in the title together with the job series
  - excluded job series, but only when *every* series on the posting is excluded
- **Screens eligibility.** It checks whether you can actually apply: open to the public, internal to your current agency, competitive status, excepted service, interchange agreements (including postings that explicitly deny them), special hiring authorities, and any extra hiring paths you list.
- **Sorts results into tiers:**
  - **Matches** pass every check.
  - **Watch list** jobs fail only on posted salary, at a grade you've chosen to keep an eye on.
  - **Near misses** fail exactly one check, shown as "Only failed check: …". Hard-no failures (wrong occupation, executive role, another agency's internal posting) never appear here.
- **Remembers between runs.** Each job is flagged **NEW** the first time it appears and **CLOSING SOON** when three days or less remain, and jobs that disappeared are noted. You can record a decision (applied, not interested, watching) so a job stops nagging you.
- **Runs safely on a schedule.** Each run works in its own folder and publishes reports only after they pass validation. A lock stops two runs from overlapping, and a failed run never overwrites the last good report or puts raw errors in anything that gets sent to you.
- **Delivers results where you'll see them.** Email through any SMTP server with both reports attached, or a short digest to Discord, Slack or ntfy. A retry never sends the same run twice.

Output is a Word briefing (`usajobs_briefing.docx`), an Excel tracker (`usajobs_tracker.xlsx`) and a `usajobs_last_run.json` summary.

## Requirements

- Python 3.11 or newer
- A free USAJOBS API key (next section)
- macOS, Linux or Windows

## 1. Get a USAJOBS API key

1. Go to the [USAJOBS developer site](https://developer.usajobs.gov/) and choose **Request an API Key** (the form is at <https://developer.usajobs.gov/apirequest/>).
2. Enter your name, email address and a short description such as "personal job search alerts".
3. USAJOBS emails you the key. It usually arrives within a few minutes but can take longer.

Keep two things: the **key** and the **email address you registered with**. The API requires both on every request (the email is sent as the `User-Agent` header), so use the same address you signed up with.

Don't paste the key into the config file or any file you might commit. Step 3 shows where it goes.

## 2. Install

```bash
git clone https://github.com/amv2/usajobs-monitor.git
cd usajobs-monitor
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e .
```

If you use [uv](https://docs.astral.sh/uv/): `uv venv && uv pip install -e .`

Check the install:

```bash
usajobs-monitor --help
```

## 3. Add your credentials

Set them as environment variables. The tool reads `USAJOBS_EMAIL` and `USAJOBS_API_KEY` and these take precedence over anything in a config file.

macOS / Linux (add to `~/.zshrc`, `~/.bashrc` or a file you `source` before running):

```bash
export USAJOBS_EMAIL="you@example.com"
export USAJOBS_API_KEY="paste-your-key-here"
```

Windows (PowerShell, saved for your user account):

```powershell
[Environment]::SetEnvironmentVariable("USAJOBS_EMAIL", "you@example.com", "User")
[Environment]::SetEnvironmentVariable("USAJOBS_API_KEY", "paste-your-key-here", "User")
```

Open a new terminal afterwards so the variables are picked up.

*(Planned, phase 3b)* Storing credentials in your operating system's keychain through the `keyring` library, with environment variables as the fallback.

## 4. Create your profile

Your profile is one config file. Start from one of the two files in the repo:

- `profiles/financial-management-dc.toml` is a ready-made profile for financial management roles in the Washington, DC area.
- `examples/usajobs-monitor.example.toml` is the commented template with every section.

```bash
mkdir -p ~/usajobs
cp profiles/financial-management-dc.toml ~/usajobs/usajobs-monitor.toml
```

Edit your copy, then validate it:

```bash
usajobs-monitor check-config --config ~/usajobs/usajobs-monitor.toml
```

`check-config` names the exact setting that's wrong (for example `filters.watch_list.grades[0]: expected an integer`) instead of failing halfway through a run, and warns about keys it doesn't recognize. Optional settings you leave out get defaults. Without `--config`, the tool looks for `./usajobs-monitor.toml`.

The format is TOML, so you can keep comments in your config. A JSON file with the same keys also works.

Every key, its default and what it does is listed in the [config reference](docs/CONFIG_REFERENCE.md). The sections are:

| Section | What it sets |
|---|---|
| `[paths]` | Where reports, run history and your decisions file live. Relative paths resolve against the config file's folder. |
| `[profile]` | Your current agency and the names postings use for it. Leave it blank if you aren't a federal employee. |
| `[search]` | What to ask USAJOBS for: locations and radius, keywords, how recent, a salary floor for the query, and the relevance signals (title terms, job series). |
| `[filters]` | Pay rules, the watch list, near-miss floors, and the exclusions. |
| `[eligibility]` | The hiring paths you qualify for: competitive or excepted service, competitive status, an interchange agreement, and extra USAJOBS hiring paths such as `vet` or `mspouse`. |
| `[report]` | The title on your reports. |
| `[scoring]` | Optional ranking weights. Leave it out to use the defaults. |
| `[delivery]` | Optional. Email, Discord, Slack or ntfy channels for the results (see step 6). |

Here is the part of the sample most people change first:

```toml
[search]
locations            = ["Washington, District of Columbia", "Arlington, Virginia"]
radius_miles         = 35
date_posted_days     = 14
target_role_keywords = ["Budget analyst", "Financial management analyst"]
role_title_terms     = ["budget", "financial", "accounting"]
target_job_series    = ["0501", "0560"]

[filters]
salary_upper_must_exceed = 197200   # posted top pay must be above this
mismatch_terms           = ["nurse", "physician", "pilot"]
excluded_job_series      = ["0510", "0511"]

[eligibility]
service                = "competitive"
has_competitive_status = true
```

How the main settings work together:

| Setting | Effect |
|---|---|
| `target_role_keywords` | Each keyword is searched in each location, and a keyword found in a posting's text counts toward relevance. |
| `search_only_keywords` | Searched to widen the net, but not counted as relevance, because many phrases show up in application boilerplate. |
| `role_title_terms`, `target_job_series` | A job needs at least one *target role signal* (a title term, a target series, or a target keyword) or it's a hard no. |
| `target_agencies` | Ranks those agencies higher. It doesn't filter anything out. |
| `salary_upper_must_exceed` | A match's posted top salary must be above this. |
| `watch_list` | Pay plans and grades you still want to see when pay is the only failing check, for example GS/GG-15. |
| `near_miss_upper_salary_must_exceed`, `near_miss_min_gs_grade` | Pay and grade floors for the near-miss list. |
| `mismatch_terms`, `mismatch_job_series` | Wrong occupation. Terms match whole words in the title only, so boilerplate like "from doctors to nurses" doesn't exclude a budget job. |
| `excluded_job_series` | Dropped only when every series on the posting is in this list, so postings that also hire under one of your series still come through. |
| `exclude_senior_executive`, `excluded_pay_plans` | Drop SES and equivalent roles, plus any agency-specific executive pay plans you add. |

**Tip:** run once, then read the near misses and the reason each one failed. They show you what each setting is filtering out, which is the fastest way to tune it.

*(Planned, phase 2)* Target "GS-14 or equivalent" in a locality and let the tool compute the salary floors from OPM's locality pay tables, instead of typing dollar figures.
*(Planned, phase 4)* `usajobs-monitor init`, a setup wizard with presets by applicant type (current federal employee, excepted service, veteran, military spouse, public, Pathways, reinstatement).

## 5. Run it

```bash
# Quick search: write reports straight to paths.reports_dir (or --output-dir), without run isolation
usajobs-monitor search --config ~/usajobs/usajobs-monitor.toml

# Full run: build reports in an isolated run folder, validate them, publish, then deliver (step 6)
usajobs-monitor run --config ~/usajobs/usajobs-monitor.toml
```

Reports land in `paths.reports_dir`, next to your config file unless you give an absolute path. The run also keeps `usajobs_job_history.json` there; that's how it knows what's new next time.

To record a decision, add a row to your decisions file (`paths.decisions_file`):

```csv
control_number,status,note
812345600,applied,submitted 10/14
812349900,not_interested,
```

Status is one of `applied`, `not_interested` or `watching`. *(Planned, phase 4)* `usajobs-monitor decide <control#> <status> [note]`.

## 6. Get results delivered

A run can send its results by email (any SMTP server), Discord, Slack or [ntfy](https://ntfy.sh) push notifications. Add a `[delivery.<channel>]` table for each one you want; with none, reports simply stay in `paths.reports_dir` (a synced folder such as iCloud Drive, OneDrive or Dropbox works for reading them on your phone).

```toml
[delivery.email]
smtp_host     = "smtp.fastmail.com"   # or smtp.gmail.com, smtp.office365.com, ...
security      = "ssl"                 # "ssl" (port 465) or "starttls" (port 587)
username      = "you@example.com"
to_addresses  = ["you@example.com"]

[delivery.discord]                    # the webhook URL comes from the environment

[delivery.ntfy]
server = "https://ntfy.sh"
```

Email carries the summary plus both reports as attachments. Discord, Slack and ntfy get a short digest: counts, the top matches with their links, and anything new. Every key is in the [config reference](docs/CONFIG_REFERENCE.md#delivery).

**Secrets go in environment variables only**, like your API key. A password or webhook URL in the config file is an error, so a config you share or commit can't leak one.

| Variable | Used by |
|---|---|
| `USAJOBS_SMTP_PASSWORD` | Email, when `username` is set. Use an app password, not your account password. |
| `USAJOBS_DISCORD_WEBHOOK_URL` | Discord (Server settings → Integrations → Webhooks) |
| `USAJOBS_SLACK_WEBHOOK_URL` | Slack (an app with incoming webhooks) |
| `USAJOBS_NTFY_TOPIC` | ntfy. Anyone who knows a topic on the public server can read it, so pick a long random one. |
| `USAJOBS_NTFY_TOKEN` | ntfy, optional access token for a protected topic |
| `USAJOBS_SMTP_USERNAME`, `USAJOBS_EMAIL_FROM`, `USAJOBS_EMAIL_TO` | Optional. Override the email addresses, so a config in a shared repository needs none. `USAJOBS_EMAIL_TO` takes a comma-separated list. |

Each channel can read a different variable name instead, for example `password_env = "FASTMAIL_APP_PASSWORD"`.

Check your setup, then look at the messages before sending anything:

```bash
usajobs-monitor check-config --config ~/usajobs/usajobs-monitor.toml    # says "ready" or what's missing, per channel
usajobs-monitor run --preview-delivery --config ~/usajobs/usajobs-monitor.toml
```

`--preview-delivery` runs the search and writes every message format (`email.eml`, `email.txt`, `discord.json`, `slack.json`, `ntfy.json`) into the run's `delivery-preview` folder without reading any secret or sending anything. After that, a plain `usajobs-monitor run` searches and sends.

How delivery behaves:

- **A failed run never sends reports.** If the search or report validation fails, each channel gets a short fixed notice and no attachments, never error text. Set `delivery.notify_on_failure = false` to skip the notice.
- **Nothing is sent twice.** Each channel records its result in the run folder (`delivery-email.json` and so on). Once a channel has accepted a run, or might have (a connection dropped mid-send), it won't send that run again. Only failures that prove nothing went out, such as a refused connection or a temporary rejection, are retried, up to `delivery.max_attempts` times.
- **"Accepted" means the server took the message.** It doesn't prove it reached your inbox or phone.
- **Retry a delivery without searching again** with `usajobs-monitor deliver <run id>` (or `deliver latest`). Channels that already accepted the run are skipped. If a channel says acceptance is uncertain, check whether the message arrived; to send it anyway, delete that channel's `delivery-<channel>.json` from the run folder and retry.
- `run` exits with 0 when the search worked and every channel accepted (or none is configured), 1 when the search failed, and 2 when delivery wasn't confirmed. Schedulers can alert on a non-zero exit.

## 7. Run it on a schedule

New postings usually go up on weekdays, and many close within a week or two, so a run every Monday, Wednesday and Friday morning is a good start. Every option below runs the same command. Use the full path to the virtual environment's `usajobs-monitor` so the scheduler doesn't depend on your shell setup.

Find that path with `which usajobs-monitor` (macOS/Linux) or `where usajobs-monitor` (Windows) while the virtual environment is active.

### Linux and macOS: cron

Run `crontab -e` and add:

```cron
# 7:00 AM Monday, Wednesday, Friday
0 7 * * 1,3,5  . $HOME/.usajobs_env && /home/you/usajobs-monitor/.venv/bin/usajobs-monitor run --config /home/you/usajobs/usajobs-monitor.toml >> /home/you/usajobs/cron.log 2>&1
```

cron doesn't read your shell profile, so put the `export` lines from steps 3 and 6 in `~/.usajobs_env` and protect that file with `chmod 600 ~/.usajobs_env`.

### macOS: launchd

cron works on macOS, but launchd also catches up on a run missed while the laptop was asleep. Save this as `~/Library/LaunchAgents/com.usajobs-monitor.plist`, replacing the paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.usajobs-monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string><string>-c</string>
    <string>. "$HOME/.usajobs_env" &amp;&amp; /Users/you/usajobs-monitor/.venv/bin/usajobs-monitor run --config /Users/you/usajobs/usajobs-monitor.toml</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/Users/you/usajobs/launchd.log</string>
  <key>StandardErrorPath</key><string>/Users/you/usajobs/launchd.log</string>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/com.usajobs-monitor.plist`.

### Windows: Task Scheduler

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Users\you\usajobs-monitor\.venv\Scripts\usajobs-monitor.exe" `
           -Argument "run --config C:\Users\you\usajobs\usajobs-monitor.toml"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Wednesday,Friday -At 7am
Register-ScheduledTask -TaskName "usajobs-monitor" -Action $action -Trigger $trigger
```

The task picks up the user environment variables you set in steps 3 and 6.

### GitHub Actions *(planned, phase 3b)*

A ready-made workflow so you can run the monitor without leaving a computer on: fork the repo, add `USAJOBS_EMAIL`, `USAJOBS_API_KEY` and your delivery variables as repository secrets, add your profile, and receive the digest by email or chat. Two things to know before you use it:

- **Use a private repository.** Your profile describes your eligibility and possibly your current agency. Anything committed to a public fork is public.
- Run history has to be carried between runs (as a cache or artifact) or every job will show as NEW. The workflow will handle this.

## Keeping secrets out of the repo

Your API key is tied to your email address. Your profile, decisions file and reports describe your job search and your eligibility. None of it belongs in version control.

**What's safe to commit:** code, tests, docs, `examples/usajobs-monitor.example.toml` and the de-personalized files in `profiles/`.

**What never is:** your own config, `.env` files, `job_decisions.csv`, `usajobs_job_history.json`, `reports/` (including the run folders and delivery previews) and logs.

The repo's `.gitignore` already covers the defaults:

```gitignore
# Personal configuration and credentials
usajobs-monitor.toml
config.json
config.toml
*.local.json
*.local.toml
usajobs_config.json
.env
.env.*
*_env

# Your search data
job_decisions.csv
usajobs_job_history.json
reports/
runs/
previews/
*.log

# Python
.venv/
__pycache__/
*.egg-info/
```

Good habits:

- **Keep your profile outside the repo**, for example in `~/usajobs/`, and pass it with `--config`. Then nothing personal can be committed by accident, whatever the `.gitignore` says.
- **Keep credentials in environment variables**, never in the profile. The tool reads `USAJOBS_EMAIL` and `USAJOBS_API_KEY` from the environment, and `check-config` warns if it finds them in the file. Delivery secrets (SMTP password, webhook URLs, ntfy topic) are read only from the environment; the config file can't hold them at all.
- **Check before your first push:** `git status --ignored` lists what's being ignored, and `git ls-files` lists what's tracked. Your config and data files should appear only in the first.
- **Add a secret scanner** if you contribute often. [gitleaks](https://github.com/gitleaks/gitleaks) works as a pre-commit hook, and GitHub's secret scanning and push protection can be turned on under the repository's **Settings → Code security**.
- **The tool never logs your key.** Errors name the setting that's missing or wrong, not its value. If you report a bug, paste `check-config` output rather than your config file.

**If you commit a key by mistake:** treat it as exposed even if you delete the commit straight away, since forks, clones and caches may already have it. Request a new key from the [USAJOBS developer site](https://developer.usajobs.gov/apirequest/), switch to it, and contact USAJOBS through the developer site to retire the old one. Then remove the file from history with [`git filter-repo`](https://github.com/newren/git-filter-repo) and force-push.

## How a run works

1. Load and validate the profile, then read credentials from the environment.
2. Run each search, paging through every result. Network errors, rate limits (HTTP 429) and server errors are retried with backoff.
3. Remove duplicates across searches by control number.
4. Review each job: hiring-path eligibility, temporary appointment, executive pay plan or title, occupation fit, excluded series, target role signal, and salary. Each failed check is recorded with a reason.
5. Sort jobs into matches, watch list and near misses, and score the matches.
6. Compare with the last run's history to flag NEW, CLOSING SOON and dropped jobs, and hide anything you marked not interested.
7. Write the reports into a fresh run folder, validate them, record file hashes in a manifest, and only then publish them to `paths.reports_dir`.
8. Deliver to each configured channel after checking the reports still match the manifest's hashes, and record each channel's result in the run folder.

## Roadmap

| Phase | Scope | State |
|---|---|---|
| 1 | Package layout, structured reason codes, config validation, generic profiles, tests, cross-platform lock | Done |
| 2 | Grade-based targeting with OPM locality pay tables | Planned |
| 3 | SMTP email, Discord/Slack/ntfy delivery with a send ledger | Done |
| 3b | GitHub Actions workflow, optional `keyring` secrets | Planned |
| 4 | `init` wizard, applicant-type presets, multiple profiles, `decide` command, HTML/CSV digests | Planned |
| 5 | Optional AI add-on: résumé fit notes and an MCP server. The core never requires AI. | Planned |

## Contributing

Issues and pull requests are welcome. Run the tests with:

```bash
python -m unittest discover -s tests
```

Please don't attach real configs, decisions files or reports to issues. Use the sample profile, or redact your copy first.

## License

MIT, copyright 2026 amv2. See [LICENSE](LICENSE).

This project isn't affiliated with or endorsed by USAJOBS or the U.S. Office of Personnel Management. It uses the public [USAJOBS API](https://developer.usajobs.gov/) under its terms of use.
