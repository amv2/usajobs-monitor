# Adding AI to usajobs-monitor

AI is optional. Everything else in the tool works without it. With AI turned on you get:

- **Fit notes:** for each job, a short note comparing it with your resume. Each required qualification is marked met, unclear or missing, and you get a fit level (strong, possible, stretch or unlikely), gaps, and resume wording to fix.
- **Ranking** (`rank = true`): stronger fits are listed first in the reports and alerts. Nothing is hidden.
- **Chat with your search** (MCP): ask an AI app about your matches, eligibility and why a job was filtered out.

There are two ways to connect AI, and you can use both:

- **The tool calls the AI** on every run and writes fit notes into your reports. You pick a provider in the `[ai]` table of your profile (options 2 to 6 below).
- **Your AI app calls the tool.** You chat in an app such as Claude, and it runs the search for you (option 1). Nothing goes in `[ai]`.

## Which one should I use?

| You have | Use | Cost | Your resume goes to |
|---|---|---|---|
| A Claude subscription and the Claude desktop app | [1. Claude app](#1-claude-app-desktop-or-claude-code) for chat, and [2. Claude Code CLI](#2-claude-code-cli) for notes on every run | Included in your subscription | Anthropic, under your subscription's terms |
| A ChatGPT Plus, Pro or Business plan | [3. OpenAI Codex CLI](#3-chatgpt-through-the-openai-codex-cli) | Included in your plan | OpenAI, under your plan's terms |
| A Google account | [4. Gemini CLI](#4-gemini-cli) | Free tier, or your Google AI plan | Google, under the Gemini CLI terms |
| A fast computer and want nothing to leave it | [5. Local model with Ollama](#5-local-model-with-ollama) | Free | Nowhere: it stays on your computer |
| An API key (pay per use): Claude, OpenAI, Gemini, OpenRouter and others | [6. API key](#6-api-key-pay-per-use) | Per use, usually a few cents a run | The API provider |
| LM Studio or another local model server | [6. API key](#local-model-servers-lm-studio-ollama-vllm), no key needed | Free | Nowhere: it stays on your computer |
| Something else | [7. Any other program or model](#7-any-other-program-or-model) | Depends | Depends |

A **subscription** is the monthly plan you sign in to the app with. An **API key** is a separate, pay-per-use account for developers. Most people already have a subscription, so options 1 to 4 need no API key. A ChatGPT or Claude subscription does not include API access.

## Before you start (options 2 to 7)

1. Install the `ai` extra. It adds PDF resume reading, and the Claude API client:

   ```bash
   pip install -e ".[ai]"
   ```

2. Add your resume and consent to your profile. Then pick a provider from the sections below:

   ```toml
   [ai]
   resume      = "resume.pdf"   # .pdf, .docx, .md or .txt, or a folder of versions
   send_resume = true           # required: without it your resume never leaves your computer
   fit_notes   = true           # add notes to every run
   rank        = true           # optional: stronger fits first
   provider    = "claude-cli"   # see below
   ```

3. **Allow the provider on this computer.** Options 2 to 5 and 7 run a program on your computer (option 6 doesn't, so skip this step for it). A profile can't do that by itself, so a profile someone shares with you can't start a program just because you ran it. You allow it once, outside the profile, by setting `USAJOBS_ALLOW_AI_PROVIDER` to the provider's name:

   ```bash
   export USAJOBS_ALLOW_AI_PROVIDER=claude-cli
   ```

   For scheduled runs, put that line in `~/.usajobs_env` (the file the [scheduling examples](../README.md#7-run-it-on-a-schedule) load). `usajobs-monitor check-config` tells you the exact value if it's missing.

4. **Try one job first.** Use a control number from your last report:

   ```bash
   usajobs-monitor fit 812345600 --preview   # shows what would be sent; sends nothing
   usajobs-monitor fit 812345600             # sends it and prints the note
   ```

Each run rates at most `max_jobs` postings (10 by default) and remembers the notes, so later runs only rate new postings.

---

## 1. Claude app (desktop or Claude Code)

**What it is:** you chat with Claude, and Claude runs your search through the tool. Good for questions like "which of today's matches fit my resume best?" or "can I apply to 812345600 as a current federal employee?".

**What you need:** a Claude subscription, the Claude desktop app or Claude Code, and the `mcp` extra.

**Setup:**

```bash
pip install -e ".[mcp]"
```

For the Claude desktop app, add this to `claude_desktop_config.json` (Settings > Developer > Edit Config) and restart the app:

```json
{
  "mcpServers": {
    "usajobs": {
      "command": "/path/to/usajobs-monitor/.venv/bin/usajobs-monitor",
      "args": ["mcp", "--profile", "default"]
    }
  }
}
```

For Claude Code:

```bash
claude mcp add usajobs -- /path/to/usajobs-monitor/.venv/bin/usajobs-monitor mcp --profile default
```

Then ask, for example: "Use usajobs to search my profile, then compare the matches with my attached resume."

**Good to know:**

- The tool never sends your resume here. You attach it in the chat when you want Claude to compare.
- This doesn't add notes to your scheduled reports. For that, use option 2. You can also use a scheduled task in the Claude desktop app that runs `usajobs-monitor run` and then asks Claude to review the new matches against your resume. The notes then appear in that task's chat instead of in the reports.
- The same MCP setup works in other apps that can run a local MCP server, such as the Codex CLI (`codex mcp add usajobs -- ...`) and the Gemini CLI (`gemini mcp add usajobs ...`).

## 2. Claude Code CLI

**What it is:** each run sends each new posting and your resume to Claude through the `claude` command, using your Claude subscription.

**What you need:** a Claude Pro or Max subscription and the Claude Code CLI. The desktop app alone isn't enough: you need the `claude` command in a terminal.

**Setup:**

1. Install it with `curl -fsSL https://claude.ai/install.sh | bash` (macOS and Linux) or see [Claude Code's setup page](https://code.claude.com/docs/en/setup).
2. Run `claude` once and sign in with your Claude account.
3. In your profile:

   ```toml
   [ai]
   provider = "claude-cli"
   # model  = "opus"   # optional; leave it out to use Claude Code's default
   ```

4. Allow it with `export USAJOBS_ALLOW_AI_PROVIDER=claude-cli`.

**Good to know:**

- The tool runs `claude -p --safe-mode --tools "" --strict-mcp-config --disable-slash-commands --no-session-persistence`. Claude gets no tools (no shell, no files, no web) and no MCP servers, ignores your own CLAUDE.md, memory and plugins, and saves no transcript with your resume in it. A plain `claude -p` could run commands, with no prompts at all if your Claude Code settings skip them, while reading job posting text from the internet. With these flags a posting can't make it do anything except write a note.
- Each posting uses some of your subscription's usage limits.
- **Scheduled runs:** the installer puts `claude` in `~/.local/bin`, which schedulers often don't search. Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.usajobs_env`. If `claude` can't be found, the run summary says so.
- **Your own command line instead:** to give the full path or extra flags, use option 7 with the same flags, for example `command = ["/Users/you/.local/bin/claude", "-p", "--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]`. `check-config` warns if a `claude` command is missing the safety flags.

## 3. ChatGPT through the OpenAI Codex CLI

**What it is:** the same as option 2, using OpenAI's `codex` command and your ChatGPT plan.

**What you need:** a ChatGPT Plus, Pro, Business, Edu or Enterprise plan (or an OpenAI API key), and the Codex CLI.

**Setup:**

1. Install it with `npm install -g @openai/codex` (or `brew install codex`).
2. Run `codex` once and choose **Sign in with ChatGPT**.
3. In your profile:

   ```toml
   [ai]
   provider = "codex-cli"
   # model  = "gpt-5"   # optional; leave it out to use Codex's default
   ```

4. Allow it with `export USAJOBS_ALLOW_AI_PROVIDER=codex-cli`.

**Good to know:** the tool runs `codex exec --sandbox read-only`, so Codex can't change files or run commands that write. Codex has no switch that turns its tools off entirely, so it can still read files on your computer if a posting tricks it into trying. If that worries you, prefer options 2, 5 or 6. Scheduled runs need the folder npm installs into on `PATH` (run `dirname "$(command -v codex)"` to find it).

## 4. Gemini CLI

**What it is:** the same as option 2, using Google's `gemini` command.

**What you need:** a Google account (the free tier has daily limits), or a Gemini API key, and the Gemini CLI.

**Setup:**

1. Install it with `npm install -g @google/gemini-cli` (or `brew install gemini-cli`).
2. Run `gemini` once and choose **Login with Google**.
3. In your profile:

   ```toml
   [ai]
   provider = "gemini-cli"
   # model  = "gemini-2.5-pro"   # optional
   ```

4. Allow it with `export USAJOBS_ALLOW_AI_PROVIDER=gemini-cli`.

**Good to know:** the tool runs Gemini in its read-only `plan` mode, so it can't change files or run commands. Like Codex, it can still read files, so the same caution applies. Check Google's terms for how the free tier uses what you send. Scheduled runs need `gemini`'s folder on `PATH`, as with Codex.

## 5. Local model with Ollama

**What it is:** a model that runs on your own computer. Nothing is sent anywhere, and it's free.

**What you need:** [Ollama](https://ollama.com) and a model. Fit notes need a reasonably capable model; on a laptop, try an 8B model first. Smaller models sometimes miss the answer format, and that posting just gets no note.

**Setup:**

1. Install Ollama, then download a model: `ollama pull llama3.1`.
2. In your profile:

   ```toml
   [ai]
   provider = "ollama"
   model    = "llama3.1"   # required: the model you pulled
   timeout_seconds = 600   # local models can be slow
   ```

3. Allow it with `export USAJOBS_ALLOW_AI_PROVIDER=ollama`.

**Good to know:** notes are usually weaker than from the hosted models above, so treat the fit level as a rough hint. LM Studio, and Ollama without the command line, work through the [built-in `openai` provider](#local-model-servers-lm-studio-ollama-vllm).

## 6. API key (pay per use)

**Claude API (built in).** Get a key from the [Claude Console](https://platform.claude.com/) and store it:

```bash
usajobs-monitor credentials set ANTHROPIC_API_KEY   # or export ANTHROPIC_API_KEY=...
```

```toml
[ai]
provider = "anthropic"          # the default
# model  = "claude-opus-5-5"
# effort = "medium"             # low, medium, high, xhigh or max
```

No `USAJOBS_ALLOW_AI_PROVIDER` is needed, because nothing runs on your computer. With several resumes, they're cached after the first posting of a run, so later postings cost less. Your data is covered by your API account's [commercial terms](https://www.anthropic.com/legal/commercial-terms).

### OpenAI, Gemini, OpenRouter and other API keys (built in)

`provider = "openai"` works with any service that offers the OpenAI Chat Completions API, which most do. It needs no extra package and runs no program on your computer. Store the key, then point `base_url` at the service:

```bash
usajobs-monitor credentials set OPENAI_API_KEY   # or export OPENAI_API_KEY=...
```

```toml
[ai]
provider = "openai"
model    = "gpt-5"                       # required: a model name the service knows
# base_url = "https://api.openai.com/v1" # the default
```

| Service | `base_url` | Key variable (default) | Example `model` |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` (default) | `OPENAI_API_KEY` | `gpt-5` |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` | `gemini-flash-latest` (always the current Flash model) |
| OpenRouter (many models, one key) | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` | `openai/gpt-5` |
| Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` | see the service's model list |
| Mistral | `https://api.mistral.ai/v1` | `MISTRAL_API_KEY` | `mistral-large-latest` |
| DeepSeek | `https://api.deepseek.com/v1` | `DEEPSEEK_API_KEY` | `deepseek-chat` (set `response_format = "json_object"`) |
| xAI | `https://api.x.ai/v1` | `XAI_API_KEY` | `grok-4` |
| Together | `https://api.together.xyz/v1` | `TOGETHER_API_KEY` | see the service's model list |

The key comes from the variable in the table, or from `api_key_env` if you set one. Model names change often, so check the service's own list. Each service's terms cover what you send.

- **JSON output.** The tool asks the model to fill in a fixed JSON schema (`response_format = "json_schema"`, the default). If the service or model can't do that, the run summary says the request was turned down: set `response_format = "json_object"` (plain JSON) or `"none"` (the schema goes in the prompt instead).
- **Effort.** `effort` is sent as `reasoning_effort` only when you set it (`low`, `medium` or `high` for most reasoning models). Leave it out for models without a reasoning setting, which reject it.
- **Cost.** Your resume comes first in every prompt, so services with automatic prompt caching (OpenAI, Gemini, DeepSeek and others) charge less for it after the first posting of a run.
- **Other services.** Any other `https` address works too, with `api_key_env` naming its key variable. Because the profile picks where your resume and key go, an address that isn't in the table (or on your computer) must also be allowed outside the profile, like a program: `export USAJOBS_ALLOW_AI_PROVIDER=https://llm.example.com/v1`. `check-config` prints the value.

### Local model servers (LM Studio, Ollama, vLLM)

Most local model servers speak the same API, so the same provider works with nothing leaving your computer. No key and no `USAJOBS_ALLOW_AI_PROVIDER` are needed for a server on `localhost`:

```toml
[ai]
provider = "openai"
base_url = "http://localhost:1234/v1"   # LM Studio; Ollama is http://localhost:11434/v1
model    = "qwen3-8b"                   # the model name the server shows
timeout_seconds = 600                   # local models can be slow
```

Start the server first (in LM Studio, the Developer tab; Ollama runs on its own). Plain `http` is allowed only for `localhost`, `127.0.0.1` and, from inside Docker, `host.docker.internal`. A model server on another computer needs `https`. If a small model's answers don't parse, try `response_format = "json_object"` or a larger model.

## 7. Any other program or model

**A program.** Any program that reads a prompt on standard input and prints the answer works:

```toml
[ai]
provider = "command"
command  = ["my-model-runner", "--json"]
```

Allow the whole command line, exactly: `export USAJOBS_ALLOW_AI_PROVIDER='my-model-runner --json'`. Allowing `python3 my_model.py` doesn't let another profile run `python3 -c ...`. The answer must contain the JSON object the prompt asks for. Use `fit --preview` to see the prompt.

**Python code.** Set `provider = "my_package.my_module:make_provider"`, where `make_provider(ai_config, env)` returns an object with `complete(system, prompt) -> str`. Allow it with the same path in `USAJOBS_ALLOW_AI_PROVIDER`. See [`fit.py`](../src/usajobs_monitor/fit.py).

---

## Privacy and safety, for every option

- Your resume is read on your computer. It's sent only when `send_resume = true`, only with postings from your own search, and only to the provider you picked.
- The posting and your resume go to the model as data, with instructions to ignore any instructions inside them. The Claude CLI option runs with every tool off; Codex and Gemini run read-only.
- A shared profile can't turn on a program-based provider by itself, or send your resume and key to an unknown `base_url`: you have to allow it with `USAJOBS_ALLOW_AI_PROVIDER`. Read any shared profile's `[ai]` table before you do.
- With `openai`, your key is sent only to `base_url`, over `https` (plain `http` only to a server on your computer), and redirects are never followed.
- Fit notes are written by a model. Fit levels are hints and can change between runs. Read the announcement before you rely on them. The AI judges minimum qualifications from your resume, not how the assessment questionnaire will place you.

## If something goes wrong

| Message | Fix |
|---|---|
| `ai.provider would run claude ... set USAJOBS_ALLOW_AI_PROVIDER=claude-cli` | Set that variable where the run happens (your shell, or `~/.usajobs_env` for schedules). |
| `the claude program was not found` | Install the CLI, or add its folder to `PATH` in `~/.usajobs_env`. |
| `ai.provider claude-cli exited with status 1` | Run `claude` once in a terminal to check you're signed in and within your usage limits. Same for `codex` and `gemini`. |
| `the AI provider did not answer with a JSON object` | The model didn't follow the format. With Ollama, try a larger model. |
| `api.openai.com rejected the API key` (or another server's name) | Check the key with `usajobs-monitor credentials status`, and that `api_key_env` names the right variable. |
| `... does not know ai.model, or ai.base_url is wrong` | Check the model name against the service's list, and that `base_url` ends the way the table above shows (usually `/v1`). |
| `... turned the request down (HTTP 400)` | The model may not do JSON schema output: set `response_format = "json_object"` or `"none"`. If you set `effort`, try without it. |
| `could not reach localhost; check ai.base_url and that the model server is running` | Start LM Studio's server (or Ollama), and check the port in `base_url`. |
| `needs ai.send_resume = true` | Add `send_resume = true` to `[ai]` once you're comfortable sending your resume to the provider. |
