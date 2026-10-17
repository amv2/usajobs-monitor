# usajobs-monitor config reference

Template: [examples/usajobs-monitor.example.toml](../examples/usajobs-monitor.example.toml). Worked example: [profiles/financial-management-dc.toml](../profiles/financial-management-dc.toml)

## Format

- **TOML** (`usajobs-monitor.toml`). Python 3.11+ reads it with the built-in `tomllib`, so there is no new dependency, and users get comments in their config.
- **JSON** with the same keys is also accepted, so an existing `usajobs_config.json` keeps working (see "Old keys" below).
- Pick a file with `--config PATH`. Default: `./usajobs-monitor.toml`.
- `usajobs-monitor check-config` validates the file and reports errors by path (for example `filters.watch_list.grades[0]: expected an integer`). Unknown keys produce a warning.

## How a posting is sorted

1. **Fetch**: every `target_role_keywords` + `search_only_keywords` entry is searched in every location (plus remote if enabled), above `salary_query_floor`.
2. **Review**: each posting collects zero or more failure reasons.
3. **Match**: no reasons.
4. **Watch list**: the *only* reason is pay, and the posting's grade range includes a `watch_list` grade at a `watch_list` pay plan.
5. **Near miss**: failed only on pay, eligibility route, or appointment type, and clears the near-miss pay and grade floors. Any **hard-no** reason (mismatch, executive, excluded series, another agency's internal posting, no role signal) keeps it off this list.
6. **Hidden**: anything marked `not_interested` in the decisions file, whatever its list.

## Credentials (environment or keychain)

| Variable | Meaning |
|---|---|
| `USAJOBS_EMAIL` | The email you registered with USAJOBS. Sent as the `User-Agent` header, as the API requires. |
| `USAJOBS_API_KEY` | Your key from developer.usajobs.gov. |

A `[credentials]` block in the file still works for backward compatibility; `check-config` warns when it is used, and the environment variables win if both exist.

With the optional extra (`pip install "usajobs-monitor[keyring]"`), any credential or delivery secret can instead live in the OS keychain under the same variable name: `usajobs-monitor credentials set USAJOBS_API_KEY`. The order is environment variable, then keychain, then the file's `[credentials]` block. Keychain entries use the service `usajobs-monitor` and the variable name as the account; a custom `password_env` (or other `*_env`) name is looked up under that name. `USAJOBS_KEYRING=0` turns keychain lookups off. `usajobs-monitor credentials status --config PATH` shows where each value comes from without printing it.

## `[paths]`

| Key | Default | Meaning |
|---|---|---|
| `reports_dir` | `"reports"` | Where the briefing (`usajobs_briefing.docx`), tracker (`usajobs_tracker.xlsx`), `last_run.json` and run history go. |
| `decisions_file` | `"job_decisions.csv"` | Your decisions by control number: `applied`, `watching`, or `not_interested` (the not-interested list; those postings are hidden on every run). Columns: `control_number,status,note`. |
| `resume_folder` | `""` | Optional. PDFs found here are listed in the briefing. |

Relative paths resolve against the config file's folder, not the current directory.

## `[profile]`

| Key | Default | Meaning |
|---|---|---|
| `current_agency` | `""` | Your current agency's name, used in report wording ("potential interchange route from …"). |
| `current_agency_terms` | `[]` | Names and acronyms that identify your agency in a posting. Internal postings ("fed-internal-search") from your agency count as eligible; from any other agency they are a hard no. |

## `[search]`

| Key | Default | Meaning |
|---|---|---|
| `locations` | required | USAJOBS location names, for example `"Arlington, Virginia"`. |
| `radius_miles` | `25` | Search radius around each location. |
| `include_remote_jobs` | `false` | Also run each keyword as a remote-only search. |
| `date_posted_days` | `14` | Only postings opened in the last N days. Match it to how often you run. |
| `salary_query_floor` | `0` | Minimum salary sent to the API. Cuts download volume; the real pay rule is in `[filters]`. |
| `target_role_keywords` | required | Searched, and each one found in a posting's text adds to its score. |
| `search_only_keywords` | `[]` | Searched only. Not relevance signals, because phrases like "human capital" appear in application boilerplate. |
| `role_title_terms` | `[]` | Words in the title that mark a relevant role. Strongest signal. |
| `target_job_series` | `[]` | Occupational series (4 digits, as strings) that mark a relevant role. |
| `target_agencies` | `[]` | Agencies to rank higher. Not a filter, and not a role signal on its own. |

A posting with no title term, no target series and no target keyword fails with "no target role signal" (hard no).

## `[target]`

Optional. Computes the pay floors in `[filters]` from OPM's General Schedule table for one grade and locality pay area, so you don't type dollar figures. Only floors you leave out of `[filters]` are filled in. `check-config` prints the values it used.

| Key | Default | Meaning |
|---|---|---|
| `grade` | required | GS grade, 1 to 15. |
| `locality` | required | OPM locality pay area: a code (`"DCB"`, `"RUS"`), the full name, or words that identify one area (`"Seattle"`, `"Washington, DC"`). `usajobs-monitor pay-table` lists them. An ambiguous name like `"San"` is an error that lists the candidates. |
| `match` | `"at_grade"` | `at_grade` or `above_grade`; see below. |
| `year` | newest bundled | Pay table year. Bundled: 2026. Leaving it out follows new tables as they ship; `check-config` warns when the calendar year has no table yet. |
| `pay_plan` | `"GS"` | Only `GS` is supported. |

What it fills in, with `top(g)` = step 10 of grade *g* in your area, after the pay cap (`top(0)` = 0):

| Key | `at_grade` | `above_grade` |
|---|---|---|
| `salary_upper_must_exceed` | `top(grade − 1)`, at most cap − 1 | `top(grade)` |
| `min_gs_grade` | `grade` | not set |
| `near_miss_upper_salary_must_exceed` | `top(grade − 2)`, at most cap − 1 | `top(grade − 1)` |
| `near_miss_min_gs_grade` | `grade − 1` | `grade` |
| `watch_list` | not set | `pay_plans = ["GS", "GG"]`, `grades = [grade]` |

The cap − 1 limit matters where the cap flattens grades: in San Francisco GS-14 step 10 is already $197,200, so a floor of exactly the cap would shut out GS-15 postings. `grade = 15`, `locality = "DCB"`, `match = "above_grade"` reproduces the original hand-typed floors (197,200, 187,093, grade 15, GS/GG-15 watch list).

Rates are base × (1 + locality %), rounded to the nearest dollar, capped at Executive Schedule level IV, which is how OPM builds its tables. Sources for each year are listed inside `src/usajobs_monitor/paytables/gs-<year>.json` and printed by `pay-table`. 2026: [Executive Order 14368](https://public-inspection.federalregister.gov/2025-23844.pdf) Schedules 1, 5 and 9, checked against OPM's [2026 GS tables](https://www.opm.gov/policy-data-oversight/pay-leave/salaries-wages/2026/general-schedule/).

**Adding a year:** copy the newest `gs-<year>.json`, replace `year`, `effective`, `sources`, `pay_cap`, the `base` table (the Executive Order's Schedule 1) and each `percent` (Schedule 9; codes and names come from OPM's locality table list), then add a few published rows to `tests/test_paytables.py`.

## `[filters]`

**Salary and grade**

| Key | Default | Meaning |
|---|---|---|
| `salary_upper_must_exceed` | `0` | A match's posted *upper* salary must be above this. The GS pay cap (197,200 in 2026) shows only roles paying above GS-15. |
| `near_miss_upper_salary_must_exceed` | `0` | Near misses must top out above this. `0` turns the floor off. |
| `near_miss_min_gs_grade` | none | For GS-scale postings (GS, GG, GM, GL), the top grade must reach this for a near miss. Other pay plans are judged by pay alone. |
| `min_gs_grade` | none | For GS-scale postings, the top grade must reach this for a match. A posting below it fails the pay rule ("top grade GS-14 is below GS-15"), so it can still be a near miss or on the watch list. Other pay plans are judged by `salary_upper_must_exceed` alone. |
| `watch_list.pay_plans` | `[]` | Pay plans for the watch list, for example `["GS", "GG"]`. |
| `watch_list.grades` | `[]` | Grades for the watch list. A posting qualifies when its grade range includes one. Empty `watch_list` = no watch list. |

Every key in this table except `watch_list` under `at_grade` can come from `[target]` instead; a value you set here always wins.

**Exclusions** (all hard no)

| Key | Default | Meaning |
|---|---|---|
| `mismatch_terms` | `[]` | Whole words in the **title** that mark the wrong occupation (`nurse` also catches `nurses`). Announcement text is ignored, so boilerplate like "from doctors to nurses" does not exclude a budget job. |
| `mismatch_job_series` | `[]` | Any of these series on the posting excludes it. |
| `excluded_job_series` | `[]` | Excluded only when **every** series on the posting is in this list, so a posting that also hires under one of your series survives. |
| `exclude_senior_executive` | `true` | Drop SES and its equivalents (ES, SL, ST, IE, EP, EX, SN, FT, FB, FX, FJ, EV, SW, RX) and executive titles. |
| `excluded_pay_plans` | `[]` | Extra pay plans to treat as executive, for agency-specific plans such as FDIC `EM`/`EO` or FHFA `LL`. |
| `senior_executive_terms` | built-in | Title phrases that mark an executive role ("deputy director"…). Setting it replaces the built-in list. |
| `temporary_terms` | built-in | Title phrases that mark a temporary, term, NTE or detail role. Permanence is decided first from the posting's appointment-type code; these only catch what the code misses. Setting it replaces the built-in list. |

Temporary appointments and eligibility gaps are failures but **not** hard no, so they can still show as near misses.

## `[eligibility]`

| Key | Default | Meaning |
|---|---|---|
| `service` | `"none"` | `competitive`, `excepted`, or `none` (not a federal employee). Excepted-service employees qualify for postings open to "fed-excepted". |
| `has_competitive_status` | `false` | Qualifies you for "fed-competitive" (merit promotion) postings. |
| `interchange_agency` | `""` | Set if your excepted-service agency has an OPM interchange agreement. Then postings that explicitly invite interchange applicants, or advertise special authorities, count as eligible unless the text denies interchange. |
| `additional_hiring_paths` | `[]` | USAJOBS hiring-path codes you qualify for, for example `vet`, `mspouse`, `disability`, `fed-transition`, `graduates`, `peace`, `land`. |

Postings open to the public always pass.

## `[report]` and `[scoring]`

| Key | Default | Meaning |
|---|---|---|
| `report.title` | `"USAJOBS Job Monitor"` | Title on the briefing and tracker. The "Active filters" paragraph is generated from this config. |
| `scoring.*` | see sample | Ranking weights and pay tiers. Optional; the defaults equal today's behavior. |

## `[delivery]`

Optional. Each `[delivery.<channel>]` table you add turns that channel on; with none, `run` reports delivery as `disabled` and reports stay in `paths.reports_dir`. Secrets are read **only** from environment variables or, with the `keyring` extra, the OS keychain under the same names: putting `password`, `webhook_url`, `topic` or `token` in the file is an error that names the variable to set instead. `check-config` reports each channel as `ready` or says what's missing, without printing values.

| Key | Default | Meaning |
|---|---|---|
| `notify_on_failure` | `true` | When a run fails, send each channel a fixed notice with no attachments. `false` sends nothing for a failed run. |
| `max_attempts` | `3` | 1 to 5. Retries happen only when the server clearly did not take the message. |
| `timeout_seconds` | `30` | 1 to 120, per connection. |
| `max_items` | `5` | 1 to 25. Jobs listed per section (matches, watch list, new near misses). Chat messages also stop at the service's length limit and say how many more are in the reports. |

### `[delivery.email]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Set `false` to keep the table but stop sending. |
| `smtp_host` | required | Your provider's SMTP server, for example `smtp.fastmail.com` or `smtp.gmail.com`. |
| `security` | `"ssl"` | `ssl` (TLS from the start) or `starttls` (upgrade a plain connection). Certificates are always verified; there is no unencrypted mode. |
| `smtp_port` | `465` for `ssl`, `587` for `starttls` | |
| `username` | `""` | SMTP login. Empty means no login (a relay that doesn't need one). Env override: `USAJOBS_SMTP_USERNAME`. |
| `password_env` | `"USAJOBS_SMTP_PASSWORD"` | The variable holding the SMTP password (use an app password). Read only when `username` is set. |
| `from_address` | the username | Sender address. Env override: `USAJOBS_EMAIL_FROM`. |
| `to_addresses` | required | Up to 10 recipients. Env override: `USAJOBS_EMAIL_TO`, comma-separated. |
| `attach_reports` | `true` | Attach the Word briefing and Excel tracker. The whole message must stay under 20 MiB; set `false` to send the summary only. |

### `[delivery.discord]` and `[delivery.slack]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | |
| `webhook_url_env` | `"USAJOBS_DISCORD_WEBHOOK_URL"` / `"USAJOBS_SLACK_WEBHOOK_URL"` | The variable holding the webhook URL. Discord URLs must be `https://discord.com/api/webhooks/…`; Slack URLs `https://hooks.slack.com/services/…`. Anything else is refused. |

Discord messages can't ping anyone (`@everyone` in a job title stays plain text) and links don't unfurl. Messages stop at 2,000 characters (Discord) or 3,500 (Slack).

### `[delivery.ntfy]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | |
| `server` | `"https://ntfy.sh"` | Your ntfy server. Must be `https://`. |
| `topic_env` | `"USAJOBS_NTFY_TOPIC"` | The variable holding the topic (1–64 letters, digits, `-` or `_`). On a public server the topic name is the only protection, so make it long and random. |
| `token_env` | `"USAJOBS_NTFY_TOKEN"` | Optional access token, sent as `Authorization: Bearer`. Left unset, no token is sent. |
| `priority` | `3` | 1 (min) to 5 (urgent). |

Tapping the notification opens the first listed job.

### Delivery records

Each channel writes `delivery-<channel>.json` into the run folder with `status` `sending`, `accepted`, `unknown` or `failed`. Anything but `failed` blocks a resend of that run to that channel. `usajobs-monitor deliver <run id>` retries only the channels that failed. The outcome of each run, with every channel's result, is in the run folder's `outcome.json` and in `usajobs_delivery_last_run.json` in `paths.reports_dir`.

## Old keys (current `usajobs_config.json`)

All current keys keep their meaning. Four move, and the old location is still read:

| Old | New |
|---|---|
| `credentials.email` / `api_key` | `USAJOBS_EMAIL` / `USAJOBS_API_KEY` env vars (file still accepted, with a warning) |
| `resume_folder` (top level) | `paths.resume_folder` |
| `user_profile.current_agency` | `profile.current_agency` |
| `filters.current_agency_terms` | `profile.current_agency_terms` |

`user_profile.home_zip` and `rounded_salary_benchmark` are ignored (unused today). To reproduce the current run exactly, add `excluded_pay_plans = ["EM", "EO", "LL"]` and `near_miss_min_gs_grade = 15`.
