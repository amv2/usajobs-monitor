# usajobs-monitor

A scheduled job search for [USAJOBS](https://www.usajobs.gov) that does the filtering a keyword search can't. You describe the jobs you want and the hiring paths you qualify for in one config file, and each run gives you a short list of postings worth reading, a watch list, and the near misses, with a reason for every job it set aside.

> **Status:** early. The core search, filtering, tracking and reports (phase 1) are ported from a personal tool that runs three times a week, delivery by email, Discord, Slack and ntfy is in, and it can run on GitHub Actions. Features marked *(planned)* aren't in the package yet. See [Roadmap](#roadmap).

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

### Or keep them in your system keychain

Instead of environment variables, you can store credentials in your operating system's encrypted vault: Keychain on macOS, Credential Manager on Windows, or Secret Service (GNOME Keyring, KWallet) on Linux. Install the optional extra, then save each value. You're prompted for it and nothing is shown as you type:

```bash
pip install -e ".[keyring]"
usajobs-monitor credentials set USAJOBS_EMAIL
usajobs-monitor credentials set USAJOBS_API_KEY
usajobs-monitor credentials status          # where each one comes from; never prints values
```

The same command stores delivery secrets from [step 6](#6-get-results-delivered), for example `usajobs-monitor credentials set USAJOBS_SMTP_PASSWORD`, and `credentials status --config your-profile.toml` lists the ones your profile needs. `credentials delete NAME` removes one.

How it fits together:

- **An environment variable always wins.** The keychain is only read for a variable that isn't set, so existing setups keep working unchanged.
- Entries are saved under the service name `usajobs-monitor`, with the variable name as the account, so you can also see or remove them in Keychain Access or Credential Manager.
- To pipe a value in from a password manager or script, use `--stdin`, which reads the first line: `your-password-cli get usajobs-key | usajobs-monitor credentials set USAJOBS_API_KEY --stdin`.
- [GitHub Actions](#github-actions) doesn't use the keychain; it keeps reading repository secrets.
- Set `USAJOBS_KEYRING=0` to stop the tool from reading the keychain. Without the extra installed, or on a computer with no keychain service (a headless server, for example), the tool simply uses environment variables.
- On macOS, if you recreate the virtual environment or upgrade Python, macOS may ask once whether Python can use the saved item. Choose **Always Allow**.

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
| `[target]` | Optional. The GS grade you're after and your OPM locality pay area. The tool turns them into the dollar pay floors for you. |
| `[filters]` | Pay rules, the watch list, near-miss floors, and the exclusions. Dollar floors set here override `[target]`. |
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

[target]
grade    = 14                                 # GS-14 or equivalent
locality = "Washington-Baltimore-Arlington"   # your OPM locality pay area

[filters]
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
| `target.grade`, `target.locality` | Sets the pay floors below from OPM's pay table for that grade and area (see [Pay floors from a GS grade](#pay-floors-from-a-gs-grade)). |
| `salary_upper_must_exceed` | A match's posted top salary must be above this. Set it yourself to override the target. |
| `watch_list` | Pay plans and grades you still want to see when pay is the only failing check, for example GS/GG-15. |
| `near_miss_upper_salary_must_exceed`, `near_miss_min_gs_grade` | Pay and grade floors for the near-miss list. |
| `mismatch_terms`, `mismatch_job_series` | Wrong occupation. Terms match whole words in the title only, so boilerplate like "from doctors to nurses" doesn't exclude a budget job. |
| `excluded_job_series` | Dropped only when every series on the posting is in this list, so postings that also hire under one of your series still come through. |
| `exclude_senior_executive`, `excluded_pay_plans` | Drop SES and equivalent roles, plus any agency-specific executive pay plans you add. |

**Tip:** run once, then read the near misses and the reason each one failed. They show you what each setting is filtering out, which is the fastest way to tune it.

### Pay floors from a GS grade

Instead of typing dollar amounts, name the grade you want and where you work. The tool looks up OPM's General Schedule pay table for that locality pay area and computes the floors. The tables ship with the tool, one per year, so no network call is made.

```toml
[target]
grade    = 15
locality = "DCB"          # an OPM code, or a name such as "Washington-Baltimore-Arlington" or "Seattle"
match    = "at_grade"     # the default; see below
# year   = 2026           # the newest bundled table by default
```

| `match` | Matches | Near misses |
|---|---|---|
| `"at_grade"` (default) | GS-scale postings (GS, GG, GM, GL) whose top grade reaches your grade, and other pay plans whose top pay beats step 10 of the grade below in your area. This is "GS-15 or equivalent". | One grade lower: top grade at least grade − 1 and top pay above step 10 of grade − 2. |
| `"above_grade"` | Only postings that pay more than step 10 of your grade in your area. GS/GG postings at your grade that fail only on pay go to the watch list. | Top grade at least your grade, and top pay above step 10 of the grade below. |

For GS-15 in Washington, DC in 2026, `above_grade` gives exactly the floors the sample profile used to type by hand: $197,200 (the pay cap) for matches and $187,093 (GS-14 step 10) for near misses. Where the pay cap makes two grades pay the same, `at_grade` keeps its floor $1 under the cap so capped postings at your grade still count.

Run `usajobs-monitor check-config` to see the dollar figures your target produces. Any floor you set under `[filters]` wins over the target, so existing configs with typed amounts behave exactly as before. To browse the tables:

```bash
usajobs-monitor pay-table             # every locality area and its percentage
usajobs-monitor pay-table Seattle     # one area's GS-1 to GS-15 rates
```

The 2026 table comes from [Executive Order 14368](https://public-inspection.federalregister.gov/2025-23844.pdf) (Schedule 1 for base rates, Schedule 9 for locality percentages, Schedule 5 for the level IV pay cap) and matches OPM's [published 2026 tables](https://www.opm.gov/policy-data-oversight/pay-leave/salaries-wages/2026/general-schedule/). Rates are computed the way OPM does it: base × (1 + locality %), rounded to the dollar, capped at Executive Schedule level IV. Special rates, LEO rates and non-GS pay systems aren't modeled; those postings are judged by their posted pay.

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

**Secrets go in environment variables or your keychain**, like your API key ([step 3](#or-keep-them-in-your-system-keychain)). A password or webhook URL in the config file is an error, so a config you share or commit can't leak one.

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

cron doesn't read your shell profile, so put the `export` lines from steps 3 and 6 in `~/.usajobs_env` and protect that file with `chmod 600 ~/.usajobs_env`. If you stored everything in the macOS keychain instead, drop the `. $HOME/.usajobs_env &&` part. The keychain is readable while you're logged in. On Linux, cron jobs usually can't reach Secret Service, so keep using the env file there.

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

Load it with `launchctl load ~/Library/LaunchAgents/com.usajobs-monitor.plist`. With credentials in the keychain you can drop `. "$HOME/.usajobs_env" &amp;&amp;` from the command.

### Windows: Task Scheduler

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Users\you\usajobs-monitor\.venv\Scripts\usajobs-monitor.exe" `
           -Argument "run --config C:\Users\you\usajobs\usajobs-monitor.toml"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Wednesday,Friday -At 7am
Register-ScheduledTask -TaskName "usajobs-monitor" -Action $action -Trigger $trigger
```

The task picks up the user environment variables you set in steps 3 and 6, and runs as you, so it can also read credentials saved with `usajobs-monitor credentials set`.

### GitHub Actions

No computer to leave on? GitHub can run the monitor on its own servers. The repository includes a workflow, [`.github/workflows/usajobs-monitor.yml`](.github/workflows/usajobs-monitor.yml), that searches and delivers on a schedule, and you can also start it by hand. A run takes a few minutes, well inside the free Actions minutes a private repository gets each month.

**Use a private copy.** Your profile describes your eligibility and possibly your current agency, and your decisions file and reports describe your job search. Anything in a public repository is public. The workflow won't run in a public repository at all, so forking this repo (forks of a public repo stay public) isn't enough.

1. **Make a private copy.** On GitHub, create a new **private** repository with no README, then push this code to it:

   ```bash
   git clone https://github.com/amv2/usajobs-monitor.git my-usajobs
   cd my-usajobs
   git remote set-url origin https://github.com/YOU/my-usajobs.git
   git push -u origin main
   ```

   You can also use **Import repository** on GitHub, which copies a public repo into a new private one.

2. **Add your profile** as `search/usajobs-monitor.toml`, starting from `examples/usajobs-monitor.example.toml`. Paths in it are relative to the `search` folder, so with the defaults the decisions file is `search/job_decisions.csv` and reports go to `search/reports` on the runner. If you list résumés, put them in a folder under `search/` and point `paths.resume_folder` at it. The `.gitignore` excludes these file names so they can't be committed by accident in a public clone, so add them on purpose:

   ```bash
   git add -f search/usajobs-monitor.toml search/job_decisions.csv
   git commit -m "Add my search profile"
   git push
   ```

   Uploading them with **Add file → Upload files** on GitHub works too. To keep the profile somewhere else in the repository, set a repository variable named `USAJOBS_CONFIG` to its path.

3. **Add your secrets** under **Settings → Secrets and variables → Actions → New repository secret**. `USAJOBS_EMAIL` and `USAJOBS_API_KEY` are required; add the delivery secrets for the channels in your profile, with the same names as in [step 6](#6-get-results-delivered): `USAJOBS_SMTP_PASSWORD`, `USAJOBS_SMTP_USERNAME`, `USAJOBS_EMAIL_FROM`, `USAJOBS_EMAIL_TO`, `USAJOBS_DISCORD_WEBHOOK_URL`, `USAJOBS_SLACK_WEBHOOK_URL`, `USAJOBS_NTFY_TOPIC` and `USAJOBS_NTFY_TOKEN`. Secrets never appear in logs. The workflow passes only these names, so leave `password_env` and the other `*_env` settings at their defaults.

4. **Try it.** Open the **Actions** tab, enable workflows if GitHub asks, pick **usajobs-monitor**, choose **Run workflow** and set mode to `preview`. That searches and writes the messages as files without sending anything. Download the run's `usajobs-reports-…` artifact to see the reports and the `delivery-preview` folder. Then run it again with mode `send`.

After that it runs on its own. Good to know:

- **Schedule.** The default is 12:00 UTC on Monday, Wednesday and Friday, which is 7 AM Eastern in winter and 8 AM in summer. GitHub schedules are always in UTC; change the `cron:` line in the workflow file to pick other days or times. Scheduled runs can start several minutes late, and GitHub runs schedules only from the default branch.
- **Seen jobs are kept in the Actions cache.** Each run restores the previous run's job history, so NEW and dropped flags work as they do on your own computer, and saves the updated history for the next run. GitHub deletes a cache nobody has used for 7 days, so after a longer gap the next run treats every job as NEW. Nothing is committed back to your repository.
- **Reports are kept as run artifacts** for 30 days. Set a repository variable `USAJOBS_REPORT_DAYS` to change that (GitHub's limit is 90 days for private repositories).
- **Decisions.** Edit `search/job_decisions.csv` in the repository (GitHub's web editor is fine) and the next run picks it up.
- **Failures.** A run that fails to search or deliver fails the workflow, and GitHub emails you about failed runs by default. Re-running a failed workflow searches again rather than resending the old run.
- **Inactivity.** GitHub can disable scheduled workflows after 60 days with no repository activity; its documentation says this applies to public repositories. If it happens, re-enable the workflow from the **Actions** tab.

## Keeping secrets out of the repo

Your API key is tied to your email address. Your profile, decisions file and reports describe your job search and your eligibility. None of it belongs in version control.

**What's safe to commit:** code, tests, docs, `examples/usajobs-monitor.example.toml` and the de-personalized files in `profiles/`.

**What never is** (in a public repository): your own config, `.env` files, `job_decisions.csv`, `usajobs_job_history.json`, `reports/` (including the run folders and delivery previews) and logs.

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
- **Keep credentials in environment variables or your keychain**, never in the profile. The tool reads `USAJOBS_EMAIL` and `USAJOBS_API_KEY` from the environment (or the keychain, with the `keyring` extra), and `check-config` warns if it finds them in the file. Delivery secrets (SMTP password, webhook URLs, ntfy topic) are read only from the environment or the keychain; the config file can't hold them at all.
- **Check before your first push:** `git status --ignored` lists what's being ignored, and `git ls-files` lists what's tracked. Your config and data files should appear only in the first.
- **Add a secret scanner** if you contribute often. [gitleaks](https://github.com/gitleaks/gitleaks) works as a pre-commit hook, and GitHub's secret scanning and push protection can be turned on under the repository's **Settings → Code security**.
- **The tool never logs your key.** Errors name the setting that's missing or wrong, not its value. If you report a bug, paste `check-config` output rather than your config file.

**If you commit a key by mistake:** treat it as exposed even if you delete the commit straight away, since forks, clones and caches may already have it. Request a new key from the [USAJOBS developer site](https://developer.usajobs.gov/apirequest/), switch to it, and contact USAJOBS through the developer site to retire the old one. Then remove the file from history with [`git filter-repo`](https://github.com/newren/git-filter-repo) and force-push.

## How a run works

1. Load and validate the profile, then read credentials from the environment, falling back to the keychain.
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
| 2 | Grade-based targeting with OPM locality pay tables | Done |
| 3 | SMTP email, Discord/Slack/ntfy delivery with a send ledger | Done |
| 3b | GitHub Actions workflow, optional OS keychain secrets via `keyring` | Done |
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
