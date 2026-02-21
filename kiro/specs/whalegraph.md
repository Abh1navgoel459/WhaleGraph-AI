# WhaleGraph AI Spec (Kiro)

## Problem

Teams need a production‑grade, non‑trading intelligence layer to monitor on‑chain behavior, detect coordinated activity, and surface pump/rugpull risk signals in real time.

## Goals

- Provide a reproducible demo of on‑chain behavioral risk detection using simulated data.
- Persist provenance data in Neo4j for graph‑native queries.
- Surface risk signals in a UI and export metrics for observability.
- Maintain clear non‑trading posture.

## Non‑Goals

- No live trading or execution.
- No real‑time blockchain ingestion for MVP.
- No complex graph algorithms or embeddings.

## Architecture

- FastAPI backend
- Synthetic transaction replay
- Neo4j graph storage
- CopilotKit UI (AG‑UI)
- Datadog custom metrics

## Core Workflow

1. Replay synthetic trades (pump vs retail scenario)
2. Write Wallet/Token/Trade nodes to Neo4j
3. Compute simple heuristics (whale concentration, coordinated wallets)
4. LLM risk summary (optional) OR deterministic fallback
5. Emit Datadog metrics
6. UI shows risk output + Copilot chat

## Data Model (Neo4j)

Nodes:
- `Wallet { address, first_seen }`
- `Token { symbol }`
- `Trade { id, amount, side, ts }`
- `RiskEvent { id, ts, pump_probability, rugpull_risk, confidence, summary }`

Relationships:
- `(Wallet)-[:TRADED]->(Trade)`
- `(Trade)-[:OF_TOKEN]->(Token)`
- `(Token)-[:HAS_RISK]->(RiskEvent)`

## Metrics (Datadog)

- `whalegraph.pump_probability`
- `whalegraph.rugpull_risk`
- `whalegraph.whale_concentration`
- `whalegraph.coordinated_wallet_count`
- `whalegraph.trade_velocity`

## UI

- Button to run scenario
- JSON risk output
- Copilot sidebar for risk Q&A

## Demo Script

1. Run pump scenario (40 trades) → observe risk scores rising
2. Run retail scenario → risk remains low
3. Show Neo4j graph and Datadog dashboard

