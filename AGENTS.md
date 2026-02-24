# AGENTS.md

## Purpose

This file captures repo-specific knowledge for working on `owui-benchmarks`, with emphasis on the `chat-ui` benchmark path used by commands like:

```bash
owb run -m 2 --model slugpath-v7-2 --headed --slow-mo 500
```

That command is the default `chat-ui` benchmark in **fixed-user mode** (because `-m 2` is specified), with a visible Playwright browser and slowed interactions for debugging.

## Local Environment

- Windows venv activation: `.venv\Scripts\activate`

## Project Structure (high-value paths)

- `benchmark/cli.py`
  - CLI entrypoint for `owb`
  - Parses `run`, `list`, `profiles`, `auth`
  - Default benchmark for `owb run` is `chat-ui`
- `benchmark/scenarios/chat_ui.py`
  - Main browser/UI benchmark implementation (`ChatUIBenchmark`)
  - Handles auth, model checks, user creation, browser pool, request execution, cleanup
- `benchmark/clients/browser_client.py`
  - Playwright automation (`BrowserClient`, `BrowserPool`)
  - Login flow, selectors, message send/stream detection timing
- `benchmark/clients/http_client.py`
  - REST client for auth/admin/model APIs
  - Creates benchmark users, verifies model availability, makes model public
- `benchmark/auth/entrypoint.py` and `benchmark/auth/authenticator.py`
  - Admin signin with fallback signup
  - Service readiness checks (`/health`)
- `benchmark/core/base.py`
  - Benchmark lifecycle: `setup -> warmup -> run -> cooldown -> teardown`
- `benchmark/core/runner.py`
  - Runs benchmark, prints summary, writes results
- `benchmark/core/config.py`
  - Loads YAML + `.env` + CLI overrides into `BenchmarkConfig`
- `config/benchmark_config.yaml`
  - Default target URL and benchmark settings (currently `iterations: 1`, target URL `http://localhost:8080`)
- `config/compute_profiles.yaml`
  - Named Docker resource profiles for the Open WebUI container

## How `owb run -m 2 --model slugpath-v7-2 --headed --slow-mo 500` Works

## CLI interpretation (`benchmark/cli.py`)

- `owb` maps to `benchmark.cli:main` (`pyproject.toml`)
- `run` without benchmark name defaults to `chat-ui`
- `-m 2`
  - Sets `max_users=2`
  - Disables auto-scale (`auto_scale = args.max_users is None` becomes `False`)
  - So this runs a **fixed concurrency** test with exactly 2 browser users
- `--model slugpath-v7-2`
  - Sets `config.chat.model = "slugpath-v7-2"`
- `--headed`
  - CLI computes `headless = not args.headed`, so browser runs visible
- `--slow-mo 500`
  - Sets Playwright `slow_mo=500` ms on browser actions

The CLI ultimately calls:

- `run_chat_ui_benchmark(...)` in `benchmark/cli.py`
- Which builds config, applies overrides, sets browser flags
- Then runs `BenchmarkRunner.run_benchmark(ChatUIBenchmark)`

## Runtime flow of `ChatUIBenchmark` (`benchmark/scenarios/chat_ui.py`)

## 1) Setup

`ChatUIBenchmark.setup()` does:

1. Installs SIGINT/SIGTERM handlers to allow graceful cleanup
2. Authenticates admin (`ensure_admin_authenticated`)
3. Verifies the requested model exists (`verify_model_available`)
4. Makes that model public (`make_model_public`) so benchmark users can access it
5. Creates benchmark users (for fixed mode, exactly 2 users here)
6. Initializes Playwright browser pool
7. Logs in browser sessions (fixed mode logs all users before the run)

## 2) Run (fixed mode because `-m 2`)

`ChatUIBenchmark.run()` dispatches to `_run_fixed()`:

- Creates `MetricsCollector`
- Uses logged-in browser clients (`2`)
- Uses `config.chat.requests_per_user` (default is `5`)
- Total requests = `users * requests_per_user` (default: `2 * 5 = 10`)
- Calls `_execute_requests(...)`

## 3) Per-user request behavior (`_execute_requests`)

For each browser user (concurrently):

- Chooses a prompt from `config.chat.prompt_pool`
- For requests after the first, clicks "New Chat"
- Sends the prompt in the UI via Playwright
- Waits for assistant streaming response to appear and stabilize
- Records:
  - total response duration (UI-observed)
  - TTFT (time to first rendered token/content)
  - approximate tokens rendered (`len(content) // 4`)
  - success/failure
- Sleeps `0.5s` between requests

All user sessions run via `asyncio.gather(...)`.

## 4) Teardown / cleanup

`ChatUIBenchmark.teardown()` always attempts cleanup:

- Closes browser contexts / Playwright
- Closes HTTP clients
- Deletes created benchmark users via admin API
- Closes admin client (if benchmark created it)

This is important when debugging in `--headed` mode because interrupting a run should still clean up users.

## What the Browser Automation Actually Measures

In `benchmark/clients/browser_client.py`, `send_message_and_wait(...)` measures UI-visible timings:

- It sends a message via the actual chat input (`contenteditable`)
- It waits for message elements to appear in the DOM
- It watches response content text growth
- TTFT is captured when response text first becomes non-empty
- Completion is inferred after content length is stable for ~500ms (`5` checks at `100ms`)

This means the metric includes:

- frontend rendering latency
- backend response latency
- streaming behavior as seen in the browser

It is not just backend API latency.

## Config + Environment Inputs That Matter Most

## CLI flags (for this command)

- `-m/--max-users`: fixed mode user count
- `--model`: model ID to test
- `--headed`: visible browser window
- `--slow-mo`: Playwright action delay (debugging)
- `-u/--url`: overrides target Open WebUI URL
- `-o/--output`: custom results dir

## YAML defaults (`config/benchmark_config.yaml`)

- `benchmark.target_url`: default `http://localhost:8080`
- `benchmark.iterations`: currently `1`
- `chat.requests_per_user`: default `5`
- `chat.response_time_threshold_ms`: used for auto-scale mode (not fixed mode)
- `browser.browser_timeout`: default `30000ms`

## Environment variables (common)

- `OPEN_WEBUI_URL` or `BENCHMARK_TARGET_URL` (target service URL)
- `ADMIN_USER_EMAIL`
- `ADMIN_USER_PASSWORD`
- `ADMIN_USER_NAME` (optional)

Auth flow will:

- wait for service readiness (`/health`)
- try admin signin
- fallback to signup (first-run instance)

## Results Output

`BenchmarkRunner` writes results under:

- `results/<benchmark_name>/<UTC timestamp>/`

For `chat-ui`, folder name becomes:

- `results/chat_ui_concurrency/<timestamp>/`

Files typically include:

- `result.json`
- `results.csv`
- `summary.txt`

## Practical Debugging Notes for This Exact Command

- `-m 2` is a good debugging setup because it avoids auto-scale complexity.
- `--headed --slow-mo 500` makes login/selectors/UI issues much easier to observe.
- If the run fails before browsers launch, common causes are:
  - missing Playwright browser install (`playwright install chromium`)
  - Open WebUI not reachable at target URL
  - bad admin creds in `.env`
  - model ID typo / model not available to admin
- If login fails in browser sessions, inspect the selectors in `BrowserClient.SELECTORS` because Open WebUI UI changes can break them.

## Important Implementation Notes / Caveats

- `BaseBenchmark.execute()` runs `setup()` and always attempts `teardown()` in `finally`, so cleanup code is part of the normal lifecycle.
- `BaseBenchmark.warmup()` is currently a no-op, so `warmup_requests` in config does not produce actual warmup traffic unless a benchmark overrides it.
- `chat-ui` fixed mode logs all browser sessions during setup; auto-scale mode logs them incrementally.
- UI benchmark metrics are derived from browser DOM observation and text stability, not a direct API completion event.
- Potential auto-scale gotcha: `BrowserPool.create_clients()` resets `self._clients` on each call, which may conflict with the incremental-add logic in `ChatUIBenchmark._run_auto_scale()`. This does not affect fixed mode (`-m ...`) runs.

## First Files To Open When Debugging `chat-ui`

1. `benchmark/scenarios/chat_ui.py`
2. `benchmark/clients/browser_client.py`
3. `benchmark/cli.py`
4. `benchmark/clients/http_client.py`
5. `benchmark/core/config.py`
6. `config/benchmark_config.yaml`
