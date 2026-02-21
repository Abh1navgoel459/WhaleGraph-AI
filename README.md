# WhaleGraph AI

Agentic on-chain behavioral intelligence (non-trading). We simulate a live trade stream, model wallet/token relationships as a graph, let an LLM agent reason over market signals, and emit full observability metrics.

## What It Does

- Streams synthetic on-chain trades in real time.
- Builds a behavior graph (wallets, tokens, trades, influence/coordination).
- Runs an agentic decision loop (paper trader) with LLM-backed explanations and deterministic fallback.
- Emits production observability metrics (Datadog) and exposes live debug panels in the UI.

## Tech Used

- **AWS Bedrock**: LLM agent for behavior classification and trade decisions.
- **Neo4j**: Graph storage for wallet/token/trade relationships + insights.
- **Datadog**: Live metrics for stream health, signals, and PnL.
- **CopilotKit + AG-UI**: Operator UI that explains agent decisions.
- **Kiro**: Specs, steering, and hooks artifacts in `kiro/`.

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set your credentials in `.env`.

Run the API:

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

## UI (CopilotKit)

```bash
cd ui
npm install
cp .env.local.example .env.local
npm run dev
```

Open http://127.0.0.1:3000

## Key Endpoints

- `GET /api/stream` — live SSE stream for the paper trader.
- `GET /api/integrations` — integration status (Bedrock/Neo4j/Datadog/CopilotKit).
- `GET /api/neo4j/summary` — graph summary.
- `GET /api/neo4j/insights` — coordination / influence insights.
- `POST /agui` — CopilotKit AG-UI runtime endpoint.

## Demo Flow (2–3 minutes)

1. Start the stream from the UI and show the live chart and JSON events.
2. Highlight agent positions, PnL, and top active tokens.
3. Show Neo4j graph history and insights (wallet influence + coordination).
4. Show Datadog metrics panel updating in real time.
5. Ask CopilotKit “why did the agent take that trade?” to show reasoning.

## Kiro Artifacts

- `kiro/specs/whalegraph.md`
- `kiro/steering.md`
- `kiro/hooks.md`
