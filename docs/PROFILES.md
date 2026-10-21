# Configuring your profile

Details for the profile file that `usajobs-monitor init` creates. Moved here from the README to keep the main page short. Start with the [README quickstart](../README.md#quickstart) if you haven't run the tool yet.

## Config sections

The format is TOML, so you can keep comments in your config. A JSON file with the same keys also works.

Every key, its default and what it does is listed in the [config reference](CONFIG_REFERENCE.md). The sections are:

| Section | What it sets |
|---|---|
| `[paths]` | Where reports, run history and your decisions file live. Relative paths resolve against the config file's folder. |
| `[profile]` | Your current agency and the names postings use for it. Leave it blank if you aren't a federal employee. |
| `[search]` | What to ask USAJOBS for: locations and radius, keywords, how recent, a salary floor for the query, and the relevance signals (title terms, job series). |
| `[target]` | Optional. The GS grade you're after and your OPM locality pay area. The tool turns them into the dollar pay floors for you. |
| `[filters]` | Pay rules, the watch list, near-miss floors, and the exclusions. Dollar floors set here override `[target]`. |
| `[eligibility]` | The hiring paths you qualify for: an applicant-type preset, or the details yourself (competitive or excepted service, competitive status, an interchange agreement, and extra USAJOBS hiring paths such as `vet` or `mspouse`). |
| `[report]` | The title on your reports and which formats to write (HTML, CSV, Word, Excel). |
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

### Applicant types

Who may apply is set per announcement through USAJOBS *hiring paths* ("This job is open to"). Instead of working out which paths apply to you, pick one or more presets:

```toml
[eligibility]
presets = ["veteran", "military-spouse"]
```

| Preset | Keeps postings open to | Rule it reflects |
|---|---|---|
| `public` | The public | [USAJOBS: open to the public](https://help.usajobs.gov/working-in-government/unique-hiring-paths/public) |
| `competitive-fed` | The public, and competitive-service employees (merit promotion) | Competitive status, [5 CFR 212.301](https://www.ecfr.gov/current/title-5/section-212.301) |
| `excepted-fed` | The public and excepted-service employees; with `interchange_agency` set, also competitive-service postings that invite interchange candidates | OPM interchange agreements, [5 CFR 6.7](https://www.ecfr.gov/current/title-5/section-6.7) |
| `veteran` | The public and veterans (`vet`) | VEOA, 5 U.S.C. 3304(f) ([OPM](https://www.opm.gov/fedshirevets/hiring-officials/strategic-recruitment-and-hiring/military-spouses-family-members/tabs/veterans-employment-opportunities-act/)); VRA and the 30% disabled authority |
| `military-spouse` | The public and military spouses (`mspouse`) | [5 CFR 315.612](https://www.ecfr.gov/current/title-5/section-315.612) |
| `student` | The public and students (`student`), the Pathways Internship Program | [5 CFR 362.202 and 362.203](https://www.ecfr.gov/current/title-5/part-362) |
| `recent-graduate` | The public and recent graduates (`graduates`), the Pathways Recent Graduates Program | [5 CFR 362.302](https://www.ecfr.gov/current/title-5/part-362): within 2 years of completing your program, up to 6 for veterans kept from applying by service |
| `reinstatement` | The public, and competitive-service postings, which accept reinstatement eligibles | [5 CFR 315.401](https://www.ecfr.gov/current/title-5/section-315.401): no time limit with career tenure or veterans' preference, otherwise 3 years after separation |
| `disability` | The public and individuals with disabilities (`disability`), Schedule A | [5 CFR 213.3102(u)](https://www.ecfr.gov/current/title-5/section-213.3102) |

Presets combine, and anything you set yourself under `[eligibility]` wins. `check-config` prints what the presets filled in. A preset only says which postings you *could* apply to; each route has its own conditions (service length, time limits, documents), so read the announcement's "Who may apply" before applying. The [config reference](CONFIG_REFERENCE.md#applicant-type-presets) has the details for each one.

### Several profiles

One install can run several searches, for example one for you and one for a spouse. Each profile is a config file in your profiles folder with its own reports, history and decisions:

| System | Profiles folder |
|---|---|
| macOS, Linux | `~/.config/usajobs-monitor/profiles` (or `$XDG_CONFIG_HOME/usajobs-monitor/profiles`) |
| Windows | `%APPDATA%\usajobs-monitor\profiles` |

Set `USAJOBS_MONITOR_HOME` to use another folder. Then:

```bash
usajobs-monitor init --profile alex           # writes profiles/alex.toml, reports go to profiles/alex/reports
usajobs-monitor init --profile sam
usajobs-monitor profiles                      # list them
usajobs-monitor run --profile sam             # run one
usajobs-monitor run --all                     # run every profile, one after another
```

Every command takes `--profile NAME` or `--config PATH`. With neither, it uses `./usajobs-monitor.toml`, or your only profile if you have just one. `run --all` refuses to start if two profiles share a reports folder.
