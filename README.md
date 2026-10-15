# Multi-Agent-System-using-LangGraph-MCP-Supervisor-Guardrails-HITL

A demo multi-agent system that uses LangGraph and MCP to implement a travel-planning assistant with a Supervisor, input Guardrails, and Human-In-The-Loop (HITL) approval flows. The project includes a FastAPI frontend, example MCP server, and client helpers to demonstrate how agents, supervisors, and guardrails can be composed into a safe, reviewable planning pipeline.

Key ideas:
- Multi-agent coordination using LangGraph and MCP
- Supervisor agent to manage complex workflows
- Input guardrails to validate user requests
- Human-in-the-loop approval for generated plans

Contents
- `app.py`: FastAPI web frontend and API endpoints
- `backend.py`: core agent orchestration / travel-planner logic
- `mcp_client.py`: client helpers to interact with the MCP server
- `custom_weather_mcp_server.py`: example MCP server for weather checks
- `templates/`, `static/`: frontend UI assets (HTML, JS, CSS)

Features
- Interactive web UI for sending travel planning prompts
- Endpoint for drafting travel plans and separate approval endpoint
- Example MCP server demonstrating domain adapters (weather, checkpoints)

Prerequisites
- Python 3.10+ (recommended)
- Git (to clone the repo)
- A virtual environment tool (venv) or similar

Quick start (Windows)

1. Create and activate a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1    # PowerShell
```

2. Install dependencies

```powershell
pip install -r requirements.txt
```

3. Run the FastAPI app (development)

```powershell
# option A (run module)
python app.py

# option B (uvicorn)
uvicorn app:app --reload --host 127.0.0.1 --port 8000
```

4. Open the web UI

Visit http://127.0.0.1:8000 in your browser to use the TripBuddy frontend.

Running the MCP server (example)
- The repository includes `custom_weather_mcp_server.py` as an example MCP server. Run it in a separate terminal if you want to experiment with custom adapters used by the demo.

```powershell
# start example MCP server (if needed)
python custom_weather_mcp_server.py
```

API Endpoints
- `POST /api/travel` — create or resume a travel planning thread. JSON: `{ "message": "<user prompt>", "thread_id": "optional-thread-id" }`
- `POST /api/travel/approve` — approve or request revisions for a draft. JSON: `{ "thread_id": "<id>", "approved": true|false, "feedback": "optional" }`
- `GET /health` — basic health check and features list

Accounts (optional, powered by Clerk)
- Guests can use everything without signing up. A signed-in user gets every solo plan and group trip saved to **My trips** (`/my-trips`) and can reopen them on any device.
- Clerk handles sign-up, log-in, Google sign-in, email verification and password reset; user accounts live at Clerk. TripBuddy's PostgreSQL stores trips and plans tagged with the Clerk user ID (`trip_members.clerk_user_id`, `solo_plans.clerk_user_id`).
- The browser sends Clerk's short-lived session token with each `/api` call. The server verifies it with Clerk's public key (RS256 signature, expiry, and that it was issued for this site) and reads the user ID from the `sub` claim. See `accounts.py`.
- `GET /api/me/trips` lists the user's group trips and solo plans; `GET /api/me/plans/{thread_id}` opens a saved solo plan.
- New columns and tables are added alongside the existing schema; nothing existing is changed.

Configuration & environment
- Secrets and API keys are not included in the repo. Use environment variables or a `.env` file for any required keys consumed by `langgraph`, `langchain`, or other adapters.
- Accounts: set `CLERK_PUBLISHABLE_KEY` in `.env` (from the Clerk dashboard's API keys page). Optional: `CLERK_JWT_KEY` (the dashboard's JWKS Public Key, PEM) to verify tokens without a network call. Without a publishable key TripBuddy runs in guest mode. Open the app at `http://localhost:8000`.
- AI model: TripBuddy uses `openai/gpt-oss-120b` through OpenRouter. Set `OPENROUTER_API_KEY` in `.env`; `LLM_MODEL` optionally picks a different OpenRouter model.

Development notes
- The project keeps synchronous convenience wrappers in `backend.py` while running an async FastAPI server — `nest_asyncio` is applied in `app.py` to allow the sync helpers to call async MCP helpers.
- Tests are not included; to experiment, interact with the web UI or call the API endpoints directly.

Contributing
- Contributions are welcome. Please open issues or pull requests for bug fixes, documentation improvements, or new adapter examples.

License
- This repository follows the license in the `LICENSE` file.

Acknowledgements
- Built as a demonstration of LangGraph + MCP patterns with supervisor and guardrail concepts.

Contact
- For questions or suggestions, open an issue or contact the repository owner.
