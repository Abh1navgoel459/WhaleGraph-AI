import json
import os
import time
import uuid
import asyncio
import math
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from jinja2 import Environment, FileSystemLoader
from neo4j import GraphDatabase
from pydantic import BaseModel
from ag_ui.core import (
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)
from ag_ui.encoder import EventEncoder

load_dotenv()

APP_NAME = "WhaleGraph AI"

# Env
NEO4J_URI = os.getenv("NEO4J_URI", "")
NEO4J_USER = os.getenv("NEO4J_USER", "")
NEO4J_PASS = os.getenv("NEO4J_PASS", "")
NEO4J_BLOOM_URL = os.getenv("NEO4J_BLOOM_URL", "")

BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-7-sonnet-20250219-v1:0")
AWS_REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-west-2"))

DD_API_KEY = os.getenv("DD_API_KEY", "")
DD_SITE = os.getenv("DD_SITE", "datadoghq.com")
DD_METRIC_NAMESPACE = os.getenv("DD_METRIC_NAMESPACE", "whalegraph")

USE_LLM = os.getenv("USE_LLM", "false").lower() == "true"
USE_DATADOG = os.getenv("USE_DATADOG", "false").lower() == "true"

app = FastAPI(title=APP_NAME)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://127.0.0.1:3000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Environment(loader=FileSystemLoader("app/templates"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_agui_event(messages: List[Dict[str, str]], thread_id: str = "stream"):
    """Append an AG-UI style event to the local log so the UI can display it."""
    global LAST_AGUI_EVENTS
    LAST_AGUI_EVENTS.append(
        {
            "ts": now_iso(),
            "thread_id": thread_id,
            "run_id": str(uuid.uuid4()),
            "messages": messages,
        }
    )
    LAST_AGUI_EVENTS = LAST_AGUI_EVENTS[-20:]


class ReplayRequest(BaseModel):
    token: str = "SOL"
    scenario: str = "pump_and_dump"  # pump_and_dump | retail_noise
    batch_size: int = 25
    include_market: bool = True
    dataset: str = "default"  # default | train | test


TRAINING_CONTEXT: Dict[str, Any] = {}
LAST_METRICS: Dict[str, Any] = {}
LAST_DECISION: Dict[str, Any] = {}
LAST_AGUI_EVENTS: List[Dict[str, Any]] = []


def get_neo4j_driver():
    if not (NEO4J_URI and NEO4J_USER and NEO4J_PASS):
        return None
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))


def neo4j_is_healthy() -> bool:
    driver = get_neo4j_driver()
    if not driver:
        return False
    try:
        with driver.session() as session:
            session.run("RETURN 1").single()
        return True
    except Exception:
        return False
    finally:
        driver.close()


@app.get("/api/neo4j/ping")
async def neo4j_ping():
    driver = get_neo4j_driver()
    if not driver:
        return JSONResponse({"ok": False, "error": "neo4j_not_configured"}, status_code=400)
    try:
        with driver.session() as session:
            session.run("RETURN 1").single()
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        driver.close()


def init_constraints(driver):
    cypher = [
        "CREATE CONSTRAINT wallet_id IF NOT EXISTS FOR (w:Wallet) REQUIRE w.address IS UNIQUE",
        "CREATE CONSTRAINT token_id IF NOT EXISTS FOR (t:Token) REQUIRE t.symbol IS UNIQUE",
        "CREATE CONSTRAINT trade_id IF NOT EXISTS FOR (tr:Trade) REQUIRE tr.id IS UNIQUE",
        "CREATE CONSTRAINT risk_id IF NOT EXISTS FOR (r:RiskEvent) REQUIRE r.id IS UNIQUE",
    ]
    with driver.session() as session:
        for q in cypher:
            session.run(q)


def write_trades(driver, trades: List[Dict[str, Any]]):
    cypher = """
    UNWIND $trades AS t
    MERGE (w:Wallet {address: t.wallet})
      ON CREATE SET w.first_seen = datetime(t.ts)
    MERGE (tk:Token {symbol: t.token})
    MERGE (tr:Trade {id: t.id})
      SET tr.amount = t.amount,
          tr.side = t.side,
          tr.ts = datetime(t.ts)
    MERGE (w)-[:TRADED]->(tr)
    MERGE (tr)-[:OF_TOKEN]->(tk)
    """
    with driver.session() as session:
        session.run(cypher, trades=trades)


def write_risk_event(driver, token: str, risk: Dict[str, Any]):
    cypher = """
    MERGE (tk:Token {symbol: $token})
    CREATE (r:RiskEvent {id: $id, ts: datetime($ts),
      pump_probability: $pump_probability,
      rugpull_risk: $rugpull_risk,
      confidence: $confidence,
      summary: $summary
    })
    MERGE (tk)-[:HAS_RISK]->(r)
    """
    with driver.session() as session:
        session.run(cypher, token=token, id=str(uuid.uuid4()), ts=now_iso(), **risk)


def write_coordination_edge(driver, wallet_a: str, wallet_b: str, token: str, reason: str):
    cypher = """
    MERGE (a:Wallet {address: $a})
    MERGE (b:Wallet {address: $b})
    MERGE (t:Token {symbol: $token})
    MERGE (a)-[r:COORDINATED_WITH]->(b)
    SET r.reason = $reason, r.token = $token, r.ts = datetime($ts)
    """
    with driver.session() as session:
        session.run(cypher, a=wallet_a, b=wallet_b, token=token, reason=reason, ts=now_iso())


def write_influence_edge(driver, wallet_a: str, wallet_b: str, token: str, reason: str):
    cypher = """
    MERGE (a:Wallet {address: $a})
    MERGE (b:Wallet {address: $b})
    MERGE (t:Token {symbol: $token})
    MERGE (a)-[r:INFLUENCES]->(b)
    SET r.reason = $reason, r.token = $token, r.ts = datetime($ts)
    """
    with driver.session() as session:
        session.run(cypher, a=wallet_a, b=wallet_b, token=token, reason=reason, ts=now_iso())


def simple_stats(trades: List[Dict[str, Any]]):
    whales = {"WHALE_1", "WHALE_2", "WHALE_3"}
    whale_trades = [t for t in trades if t["wallet"] in whales]
    total_amount = sum(t["amount"] for t in trades) or 1
    whale_amount = sum(t["amount"] for t in whale_trades)
    whale_concentration = int((whale_amount / total_amount) * 100)
    coordinated_wallets = len({t["wallet"] for t in trades if t.get("coordinated")})
    trade_velocity = len(trades)
    return whale_concentration, coordinated_wallets, trade_velocity


def simulate_market_snapshot(token: str, stats: Dict[str, Any]) -> Dict[str, Any]:
    # Simple deterministic stub based on risk signals (non-trading)
    base_price = 120.0
    drift = (stats["whale_concentration"] * 0.02) + (stats["coordinated_wallets"] * 0.5)
    price = round(base_price + drift, 2)
    volume = int(50000 + stats["trade_velocity"] * 120 + stats["coordinated_wallets"] * 800)
    return {
        "token": token,
        "price_usd": price,
        "volume_24h": volume,
        "volatility_hint": "elevated" if stats["coordinated_wallets"] > 0 else "normal",
    }


def load_dataset(dataset: str) -> List[Dict[str, Any]]:
    if dataset == "train":
        path = "app/data/synthetic_trades_train.jsonl"
    elif dataset == "test":
        path = "app/data/synthetic_trades_test.jsonl"
    else:
        path = "app/data/synthetic_trades.jsonl"
    with open(path, "r") as f:
        return [json.loads(line) for line in f.readlines()]


def summarize_training(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    stats = compute_window_stats(trades[-500:]) if trades else {}
    whales = {"WHALE_1", "WHALE_2", "WHALE_3", "WHALE_4", "WHALE_5"}
    whale_vol = sum(t["amount"] for t in trades if t["wallet"] in whales)
    total_vol = sum(t["amount"] for t in trades) or 1
    buy_ratio = sum(1 for t in trades if t["side"] == "buy") / max(1, len(trades))
    return {
        "window_stats": stats,
        "whale_volume_ratio": round(whale_vol / total_vol, 3),
        "buy_ratio": round(buy_ratio, 3),
        "trade_count": len(trades),
    }


def preload_training_context():
    global TRAINING_CONTEXT
    try:
        trades = load_dataset("train")
        TRAINING_CONTEXT = summarize_training(trades)
    except Exception:
        TRAINING_CONTEXT = {}


@app.on_event("startup")
async def _startup():
    preload_training_context()


def call_bedrock_classify(wallet_trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Simple, fast: one wallet at a time
    prompt = {
        "wallet_trades": wallet_trades[-10:],
        "schema": {
            "wallet_type": "whale|retail|coordinated_cluster|bot_like",
            "accumulation_score": "0-100",
            "manipulation_risk": "0-100",
            "reasoning": "short"
        }
    }
    if not USE_LLM:
        # deterministic stub for speed
        amt = sum(t["amount"] for t in wallet_trades)
        wallet = wallet_trades[-1]["wallet"] if wallet_trades else "UNKNOWN"
        if wallet.startswith("WHALE") or amt > 5000:
            return {
                "wallet_type": "whale",
                "accumulation_score": 80,
                "manipulation_risk": 60,
                "reasoning": "Large trade sizes and repeated accumulation."
            }
        return {
            "wallet_type": "retail",
            "accumulation_score": 20,
            "manipulation_risk": 10,
            "reasoning": "Small sporadic trades."
        }

    try:
        client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
        sys = "You are a careful classifier. Output only valid JSON."
        user = (
            "Classify the wallet behavior."
            "Return JSON with keys: wallet_type, accumulation_score, manipulation_risk, reasoning."
            f"\nDATA: {json.dumps(prompt)}"
        )
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "temperature": 0.2,
            "system": sys,
            "messages": [{"role": "user", "content": user}],
        }
        resp = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body))
        txt = json.loads(resp["body"].read())["content"][0]["text"]
        return json.loads(txt)
    except Exception:
        amt = sum(t["amount"] for t in wallet_trades)
        wallet = wallet_trades[-1]["wallet"] if wallet_trades else "UNKNOWN"
        if wallet.startswith("WHALE") or amt > 5000:
            return {
                "wallet_type": "whale",
                "accumulation_score": 80,
                "manipulation_risk": 60,
                "reasoning": "Large trade sizes and repeated accumulation.",
            }
        return {
            "wallet_type": "retail",
            "accumulation_score": 20,
            "manipulation_risk": 10,
            "reasoning": "Small sporadic trades.",
        }


def call_bedrock_risk_summary(stats: Dict[str, Any]) -> Dict[str, Any]:
    if not USE_LLM:
        pump = min(100, stats["whale_concentration"] + stats["coordinated_wallets"] * 10)
        rug = min(100, stats["coordinated_wallets"] * 12)
        return {
            "pump_probability": pump,
            "rugpull_risk": rug,
            "confidence": 65,
            "summary": "Rising whale concentration and coordinated wallets increase pump risk."
        }

    try:
        client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
        sys = "You are a risk analyst. Output only valid JSON."
        user = (
            "Given these signals, return JSON with keys:"
            " pump_probability, rugpull_risk, confidence, summary."
            f"\nSIGNALS: {json.dumps(stats)}"
        )
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 300,
            "temperature": 0.2,
            "system": sys,
            "messages": [{"role": "user", "content": user}],
        }
        resp = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body))
        txt = json.loads(resp["body"].read())["content"][0]["text"]
        return json.loads(txt)
    except Exception:
        pump = min(100, stats["whale_concentration"] + stats["coordinated_wallets"] * 10)
        rug = min(100, stats["coordinated_wallets"] * 12)
        return {
            "pump_probability": pump,
            "rugpull_risk": rug,
            "confidence": 65,
            "summary": "Rising whale concentration and coordinated wallets increase pump risk.",
        }


def emit_datadog_metrics(token: str, stats: Dict[str, Any], risk: Dict[str, Any]):
    if not USE_DATADOG or not DD_API_KEY:
        return
    ts = int(time.time())
    series = [
        {
            "metric": f"{DD_METRIC_NAMESPACE}.pump_probability",
            "points": [[ts, risk["pump_probability"]]],
            "tags": [f"token:{token}", "env:hackathon"],
            "type": "gauge",
        },
        {
            "metric": f"{DD_METRIC_NAMESPACE}.rugpull_risk",
            "points": [[ts, risk["rugpull_risk"]]],
            "tags": [f"token:{token}", "env:hackathon"],
            "type": "gauge",
        },
        {
            "metric": f"{DD_METRIC_NAMESPACE}.whale_concentration",
            "points": [[ts, stats["whale_concentration"]]],
            "tags": [f"token:{token}", "env:hackathon"],
            "type": "gauge",
        },
        {
            "metric": f"{DD_METRIC_NAMESPACE}.coordinated_wallet_count",
            "points": [[ts, stats["coordinated_wallets"]]],
            "tags": [f"token:{token}", "env:hackathon"],
            "type": "gauge",
        },
        {
            "metric": f"{DD_METRIC_NAMESPACE}.trade_velocity",
            "points": [[ts, stats["trade_velocity"]]],
            "tags": [f"token:{token}", "env:hackathon"],
            "type": "gauge",
        },
    ]
    payload = {"series": series}
    url = f"https://api.{DD_SITE}/api/v1/series"
    headers = {"DD-API-KEY": DD_API_KEY, "Content-Type": "application/json"}
    httpx.post(url, headers=headers, json=payload, timeout=5.0)


def emit_trader_metrics(token: str, trader: Dict[str, Any]):
    if not USE_DATADOG or not DD_API_KEY:
        return
    ts = int(time.time())
    series = [
        {
            "metric": f"{DD_METRIC_NAMESPACE}.paper.pnl",
            "points": [[ts, trader.get("pnl", 0.0)]],
            "tags": [f"token:{token}", "env:hackathon"],
            "type": "gauge",
        },
        {
            "metric": f"{DD_METRIC_NAMESPACE}.paper.position",
            "points": [[ts, len([k for k, v in trader.get("positions", {}).items() if v == 1])]],
            "tags": [f"token:{token}", "env:hackathon"],
            "type": "gauge",
        },
        {
            "metric": f"{DD_METRIC_NAMESPACE}.paper.trade_count",
            "points": [[ts, trader.get("trades", 0)]],
            "tags": [f"token:{token}", "env:hackathon"],
            "type": "count",
        },
    ]
    payload = {"series": series}
    url = f"https://api.{DD_SITE}/api/v1/series"
    headers = {"DD-API-KEY": DD_API_KEY, "Content-Type": "application/json"}
    httpx.post(url, headers=headers, json=payload, timeout=5.0)


def bedrock_chat_text(messages: List[Dict[str, str]]) -> str:
    if not USE_LLM:
        if LAST_METRICS:
            token = LAST_METRICS.get("token", "UNKNOWN")
            ws = LAST_METRICS.get("window_stats", {})
            bp = ws.get("buy_pressure", 0)
            wc = ws.get("whale_concentration", 0)
            cw = ws.get("coordinated_wallets", 0)
            return (
                f"Fallback explanation for {token}: buy_pressure={bp}, "
                f"whale_concentration={wc}, coordinated_wallets={cw}. "
                "Higher whale concentration and sustained buy pressure increase pump risk; "
                "coordination spikes increase rugpull risk."
            )
        return "Fallback explanation: risk is derived from buy pressure, whale concentration, and coordination."

    client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    sys = (
        "You are WhaleGraph Copilot. Explain pump/rugpull risk based on user questions. "
        "Be concise and data-driven."
    )
    user = "\n".join([m.get("content", "") for m in messages if m.get("role") == "user"]).strip()
    if not user:
        user = "Provide a concise risk explanation based on the latest stream signals."
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 300,
        "temperature": 0.2,
        "system": sys,
        "messages": [{"role": "user", "content": user}],
    }
    resp = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body))
    txt = json.loads(resp["body"].read())["content"][0]["text"]
    return txt.strip()


def bedrock_trader_decision(context: Dict[str, Any]) -> Dict[str, Any]:
    if not USE_LLM:
        return {}
    client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    sys = (
        "You are a trading-simulator agent. Output only valid JSON. "
        "Decide action based on signals. This is a paper-trading simulation, not real trading."
    )
    user = (
        "Given the context, decide one action: enter | exit | hold, and provide a brief reason. "
        "Return JSON with keys: action, reason.\n"
        f"CONTEXT: {json.dumps(context)}"
    )
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 120,
        "temperature": 0.2,
        "system": sys,
        "messages": [{"role": "user", "content": user}],
    }
    resp = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body))
    txt = json.loads(resp["body"].read())["content"][0]["text"]
    return json.loads(txt)


def compute_window_stats(window: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not window:
        return {
            "buy_pressure": 0.0,
            "whale_concentration": 0,
            "coordinated_wallets": 0,
            "coord_ratio": 0.0,
        }
    whales = {"WHALE_1", "WHALE_2", "WHALE_3"}
    buy_amt = sum(t["amount"] for t in window if t["side"] == "buy")
    sell_amt = sum(t["amount"] for t in window if t["side"] == "sell")
    total_amt = buy_amt + sell_amt or 1
    buy_pressure = (buy_amt - sell_amt) / total_amt
    whale_amt = sum(t["amount"] for t in window if t["wallet"] in whales)
    whale_concentration = int((whale_amt / total_amt) * 100)
    coord_wallets = {t["wallet"] for t in window if t.get("coordinated")}
    coordinated_wallets = len(coord_wallets)
    coord_ratio = coordinated_wallets / max(1, len({t["wallet"] for t in window}))
    return {
        "buy_pressure": round(buy_pressure, 3),
        "whale_concentration": whale_concentration,
        "coordinated_wallets": coordinated_wallets,
        "coord_ratio": round(coord_ratio, 3),
    }


def update_price(last_price: float, trade: Dict[str, Any]) -> float:
    # Deterministic price impact for simulation
    direction = 1 if trade["side"] == "buy" else -1
    impact = min(2.5, math.log1p(trade["amount"]) * 0.3)
    return max(0.01, round(last_price + direction * impact, 2))


def token_universe() -> List[Dict[str, Any]]:
    # 100 real crypto symbols with tiered base prices for simulation
    symbols = [
        "BTC","ETH","USDT","BNB","SOL","XRP","USDC","ADA","DOGE","AVAX",
        "TRX","DOT","MATIC","LINK","TON","SHIB","LTC","BCH","UNI","ATOM",
        "XLM","ETC","NEAR","ICP","APT","FIL","HBAR","VET","IMX","INJ",
        "OP","ARB","GRT","AAVE","SUI","RNDR","MKR","EGLD","ALGO","KAS",
        "FLOW","THETA","RUNE","FTM","SAND","MANA","AXS","CHZ","NEO","KAVA",
        "ZEC","DYDX","SNX","ENJ","BAT","KSM","LDO","CRV","COMP","ZIL",
        "MINA","ROSE","CELO","1INCH","GMX","STX","WAVES","ANKR","QTUM","IOTA",
        "EOS","XTZ","XMR","DASH","CAKE","GALA","LRC","HOT","RSR","YFI",
        "BAND","OMG","AMP","CSPR","KLAY","SKL","FET","AGIX","OCEAN","HNT",
        "FLUX","ICX","BAL","SUSHI","ENS","CVX","SSV","AR","JASMY","TRB"
    ]
    tokens = []
    for i, symbol in enumerate(symbols):
        if i < 5:
            base = 200 + i * 20
        elif i < 20:
            base = 30 + i
        elif i < 50:
            base = 5 + (i % 10)
        else:
            base = 0.3 + (i % 10) * 0.2
        tokens.append({"symbol": symbol, "base": float(base)})
    return tokens


def pick_token(tokens: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Weighted: large caps get more flow
    r = random.random()
    if r < 0.25:
        return tokens[0]
    if r < 0.40:
        return tokens[1]
    if r < 0.65:
        return random.choice(tokens[2:10])
    if r < 0.85:
        return random.choice(tokens[10:40])
    return random.choice(tokens[40:])


def trader_decision(
    state: Dict[str, Any],
    token: str,
    window_stats: Dict[str, Any],
    price: float,
    momentum: float,
    volatility: float,
) -> Dict[str, Any]:
    # Intuitive but non-trivial rule set for paper trading (non-executing)
    buy_pressure = window_stats["buy_pressure"]
    whale_conc = window_stats["whale_concentration"]
    coord_ratio = window_stats["coord_ratio"]
    coord_wallets = window_stats["coordinated_wallets"]

    rugpull_risk = min(100, int((coord_wallets * 12) + max(0, -buy_pressure) * 60))
    pump_raw = (whale_conc * 0.8) + (buy_pressure * 50) + (momentum * 400)
    pump_signal = max(0, min(100, int(pump_raw)))

    action = "hold"
    reason = "No strong signal."

    # Entry rule: accumulation + momentum + controlled coordination + low volatility
    position = state["positions"].get(token, 0)
    if position == 0 and state["cooldown"] == 0:
        primary = whale_conc >= 25 and buy_pressure > 0.04 and momentum > 0.004 and volatility < 0.18
        secondary = coord_wallets >= 2 and buy_pressure > 0.06
        tertiary = pump_signal >= 35 and buy_pressure > 0.02
        if (primary or secondary or tertiary) and coord_ratio <= 0.7:
            action = "enter"
            reason = "Accumulation signal or coordination spike with supportive pressure."

    # Exit rules: spike in coordination risk or drawdown or momentum reversal
    if position == 1:
        max_price = state["max_price"].get(token, price)
        drawdown = (max_price - price) / max(0.01, max_price)
        if rugpull_risk > 70 or coord_ratio > 0.6:
            action = "exit"
            reason = "Coordination spike and rugpull risk elevated."
        elif drawdown > 0.035:
            action = "exit"
            reason = "Trailing drawdown exceeded threshold."
        elif momentum < -0.01 and buy_pressure < -0.1:
            action = "exit"
            reason = "Momentum reversal with sell pressure."
        elif state["hold_ticks"].get(token, 0) > 30:
            action = "exit"
            reason = "Time-based exit to reduce exposure."

    return {
        "action": action,
        "reason": reason,
        "pump_signal": pump_signal,
        "rugpull_risk": rugpull_risk,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    tpl = templates.get_template("index.html")
    return HTMLResponse(tpl.render(app_name=APP_NAME, bloom_url=NEO4J_BLOOM_URL))


@app.post("/api/replay")
async def replay(req: ReplayRequest):
    all_trades = load_dataset(req.dataset)

    # scenario filtering
    if req.scenario == "retail_noise":
        trades = [t for t in all_trades if t.get("scenario") == "retail_noise"]
    else:
        trades = [t for t in all_trades if t.get("scenario") == "pump_and_dump"]

    # batch
    trades = trades[: req.batch_size]
    for t in trades:
        t["token"] = req.token

    driver = get_neo4j_driver()
    if driver:
        init_constraints(driver)
        write_trades(driver, trades)

    whale_concentration, coordinated_wallets, trade_velocity = simple_stats(trades)
    stats = {
        "token": req.token,
        "whale_concentration": whale_concentration,
        "coordinated_wallets": coordinated_wallets,
        "trade_velocity": trade_velocity,
    }

    # Example: classify just the last wallet
    last_wallet = trades[-1]["wallet"] if trades else "UNKNOWN"
    wallet_trades = [t for t in trades if t["wallet"] == last_wallet]
    wallet_class = call_bedrock_classify(wallet_trades)

    risk = call_bedrock_risk_summary(stats)

    if driver:
        write_risk_event(driver, req.token, risk)
        driver.close()

    emit_datadog_metrics(req.token, stats, risk)

    market = simulate_market_snapshot(req.token, stats) if req.include_market else None

    return JSONResponse({
        "token": req.token,
        "scenario": req.scenario,
        "dataset": req.dataset,
        "stats": stats,
        "wallet_classification": wallet_class,
        "risk": risk,
        "market": market,
        "bloom_url": NEO4J_BLOOM_URL,
        "ts": now_iso(),
    })


@app.get("/api/market/{token}")
async def market(token: str):
    # Stateless stub for demo
    stats = {
        "whale_concentration": 35,
        "coordinated_wallets": 3,
        "trade_velocity": 40,
    }
    return simulate_market_snapshot(token.upper(), stats)


# Training context is preloaded at startup. This endpoint is kept for debugging.
@app.post("/api/train")
async def train_agent(dataset: str = "train"):
    trades = load_dataset(dataset)
    global TRAINING_CONTEXT
    TRAINING_CONTEXT = summarize_training(trades)
    return {"ok": True, "dataset": dataset, "context": TRAINING_CONTEXT}


@app.get("/api/integrations")
async def integrations():
    return {
        "neo4j": neo4j_is_healthy(),
        "datadog": bool(DD_API_KEY and USE_DATADOG),
        "copilotkit": True,  # AG-UI endpoint is available when backend is running
        "bedrock": bool(USE_LLM),
        "model_id": BEDROCK_MODEL_ID,
    }


@app.get("/api/agent/metrics")
async def agent_metrics():
    return {
        "metrics": LAST_METRICS,
        "decision": LAST_DECISION,
    }


@app.get("/api/agui/events")
async def agui_events():
    return {"events": LAST_AGUI_EVENTS}




@app.get("/api/neo4j/summary")
async def neo4j_summary():
    driver = get_neo4j_driver()
    if not driver:
        return JSONResponse({"error": "neo4j_not_configured"}, status_code=400)
    try:
        with driver.session() as session:
            counts = session.run(
                """
                MATCH (w:Wallet) WITH count(w) AS wallets
                MATCH (t:Token) WITH wallets, count(t) AS tokens
                MATCH (tr:Trade) WITH wallets, tokens, count(tr) AS trades
                RETURN wallets, tokens, trades
                """
            ).single()
            top_wallets = session.run(
                """
                MATCH (w:Wallet)-[:TRADED]->(tr:Trade)
                WITH w, sum(tr.amount) AS total
                RETURN w.address AS wallet, total
                ORDER BY total DESC
                LIMIT 5
                """
            ).data()
            top_tokens = session.run(
                """
                MATCH (tr:Trade)-[:OF_TOKEN]->(t:Token)
                WITH t, count(tr) AS trades
                RETURN t.symbol AS token, trades
                ORDER BY trades DESC
                LIMIT 5
                """
            ).data()
        return {
            "counts": dict(counts) if counts else {},
            "top_wallets": top_wallets,
            "top_tokens": top_tokens,
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        driver.close()


@app.get("/api/neo4j/insights")
async def neo4j_insights():
    driver = get_neo4j_driver()
    if not driver:
        return JSONResponse({"error": "neo4j_not_configured"}, status_code=400)
    try:
        with driver.session() as session:
            top_influencers = session.run(
                """
                MATCH (a:Wallet)-[r:INFLUENCES]->(b:Wallet)
                WITH a, count(r) AS influence
                RETURN a.address AS wallet, influence
                ORDER BY influence DESC
                LIMIT 5
                """
            ).data()
            coord_clusters = session.run(
                """
                MATCH (a:Wallet)-[r:COORDINATED_WITH]->(b:Wallet)
                WITH a, count(r) AS c
                RETURN a.address AS wallet, c AS coordinated_edges
                ORDER BY coordinated_edges DESC
                LIMIT 5
                """
            ).data()
            risky_tokens = session.run(
                """
                MATCH (t:Token)-[:HAS_RISK]->(r:RiskEvent)
                WITH t, avg(r.pump_probability) AS pump, avg(r.rugpull_risk) AS rug
                RETURN t.symbol AS token, pump, rug
                ORDER BY (pump + rug) DESC
                LIMIT 5
                """
            ).data()
        return {
            "top_influencers": top_influencers,
            "coordination_clusters": coord_clusters,
            "risky_tokens": risky_tokens,
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        driver.close()


@app.get("/api/health")
async def health():
    return {"ok": True, "ts": now_iso()}


@app.get("/info")
async def agui_info():
    return {
        "agents": [
            {
                "name": "default",
                "description": "WhaleGraph Copilot (Bedrock-backed)",
            }
        ]
    }


@app.get("/agents__unsafe_dev_only")
async def agui_agents_dev():
    return {"agents": ["default"]}


# Some CopilotKit builds call these under the runtimeUrl prefix (e.g. /agui/info)
@app.get("/agui/info")
async def agui_info_prefixed():
    return await agui_info()


@app.get("/agui/agents__unsafe_dev_only")
async def agui_agents_dev_prefixed():
    return await agui_agents_dev()


@app.post("/agui")
async def agui_endpoint(request: Request):
    accept_header = request.headers.get("accept")
    encoder = EventEncoder(accept=accept_header)

    try:
        payload = await request.json()
    except Exception as exc:
        return JSONResponse({"error": f"invalid_agui_payload: {exc}"}, status_code=400)

    def extract_messages(obj: Dict[str, Any]) -> List[Dict[str, str]]:
        candidates = []
        if isinstance(obj, dict):
            if isinstance(obj.get("messages"), list):
                candidates = obj["messages"]
            elif isinstance(obj.get("input"), dict) and isinstance(obj["input"].get("messages"), list):
                candidates = obj["input"]["messages"]
        cleaned = []
        for m in candidates:
            if isinstance(m, dict):
                role = m.get("role") or ""
                content = m.get("content") or ""
                cleaned.append({"role": role, "content": content})
        return cleaned

    thread_id = (
        payload.get("thread_id")
        or payload.get("threadId")
        or payload.get("thread")
        or str(uuid.uuid4())
    )
    run_id = (
        payload.get("run_id")
        or payload.get("runId")
        or payload.get("run")
        or str(uuid.uuid4())
    )
    messages = extract_messages(payload)
    global LAST_AGUI_EVENTS
    LAST_AGUI_EVENTS.append(
        {
            "ts": now_iso(),
            "thread_id": thread_id,
            "run_id": run_id,
            "messages": messages,
        }
    )
    LAST_AGUI_EVENTS = LAST_AGUI_EVENTS[-20:]

    async def event_generator():
        try:
            yield encoder.encode(
                RunStartedEvent(
                    type=EventType.RUN_STARTED,
                    thread_id=thread_id,
                    run_id=run_id,
                )
            )

            message_id = str(uuid.uuid4())
            yield encoder.encode(
                TextMessageStartEvent(
                    type=EventType.TEXT_MESSAGE_START,
                    message_id=message_id,
                    role="assistant",
                )
            )

            reply = bedrock_chat_text(messages)

            yield encoder.encode(
                TextMessageContentEvent(
                    type=EventType.TEXT_MESSAGE_CONTENT,
                    message_id=message_id,
                    delta=reply,
                )
            )
            yield encoder.encode(
                TextMessageEndEvent(
                    type=EventType.TEXT_MESSAGE_END,
                    message_id=message_id,
                )
            )
            yield encoder.encode(
                RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id=thread_id,
                    run_id=run_id,
                )
            )
        except Exception as exc:
            yield encoder.encode(
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=str(exc),
                )
            )

    return StreamingResponse(event_generator(), media_type=encoder.get_content_type())


@app.get("/api/stream")
async def stream(
    token: str = "SOL",
    scenario: str = "pump_and_dump",
    speed_ms: int = 250,
    dataset: str = "test",
    multi: bool = True,
):
    all_trades = load_dataset(dataset)
    trades = [t for t in all_trades if t.get("scenario") == scenario]
    universe = token_universe()
    for t in trades:
        if multi:
            choice = pick_token(universe)
            t["token"] = choice["symbol"]
            t["base_price"] = choice["base"]
        else:
            t["token"] = token

    state = {
        "positions": {},  # token -> 0/1
        "entry_price": {},  # token -> price
        "max_price": {},  # token -> price
        "hold_ticks": {},  # token -> ticks
        "cooldown": 0,
        "pnl": 0.0,
        "trades": 0,
    }

    window: Dict[str, List[Dict[str, Any]]] = {}
    price_window: Dict[str, List[float]] = {}
    price_map: Dict[str, float] = {}
    recent_counts: Dict[str, int] = {}
    last_wallet_by_token: Dict[str, str] = {}
    recent_whale_by_token: Dict[str, Dict[str, Any]] = {}

    async def event_stream():
        driver = get_neo4j_driver()
        if driver:
            init_constraints(driver)
        tick = 0
        last_llm_decision: Dict[str, Any] = {}
        llm_available = USE_LLM
        for t in trades:
            tick += 1
            tok = t["token"]
            if tok not in price_map:
                price_map[tok] = float(t.get("base_price", 120.0))
                window[tok] = []
                price_window[tok] = []
                recent_counts[tok] = 0

            price_map[tok] = update_price(price_map[tok], t)
            price = price_map[tok]

            window[tok].append(t)
            price_window[tok].append(price)
            recent_counts[tok] = recent_counts.get(tok, 0) + 1
            if len(window[tok]) > 20:
                window[tok].pop(0)
            if len(price_window[tok]) > 20:
                price_window[tok].pop(0)

            window_stats = compute_window_stats(window[tok])
            avg_price = sum(price_window[tok]) / len(price_window[tok])
            momentum = (price - avg_price) / max(0.01, avg_price)
            variance = sum((p - avg_price) ** 2 for p in price_window[tok]) / len(price_window[tok])
            volatility = math.sqrt(variance) / max(0.01, avg_price)

            decision = trader_decision(state, tok, window_stats, price, momentum, volatility)
            llm_decision = {}
            if llm_available and tick % 20 == 0:
                llm_context = {
                    "token": tok,
                    "price": price,
                    "window_stats": window_stats,
                    "momentum": round(momentum, 4),
                    "volatility": round(volatility, 4),
                    "position": state["positions"].get(tok, 0),
                    "pnl": round(state["pnl"], 2),
                    "training_context": TRAINING_CONTEXT,
                }
                try:
                    llm_decision = bedrock_trader_decision(llm_context)
                    last_llm_decision = llm_decision
                except Exception:
                    llm_decision = {}
                    llm_available = False
            elif llm_available and last_llm_decision:
                llm_decision = last_llm_decision

            if not llm_decision:
                llm_decision = {
                    "action": decision.get("action"),
                    "reason": "Decision based on live signals.",
                }

            # If LLM decision is valid, use it; otherwise fallback to rule-based
            action = llm_decision.get("action") if isinstance(llm_decision, dict) else None
            if action in ("enter", "exit", "hold"):
                decision["action"] = action
                decision["reason"] = llm_decision.get("reason", decision["reason"])

            if state["cooldown"] > 0:
                state["cooldown"] -= 1

            if state["positions"].get(tok, 0) == 1:
                state["hold_ticks"][tok] = state["hold_ticks"].get(tok, 0) + 1
                state["max_price"][tok] = max(state["max_price"].get(tok, price), price)

            # portfolio-level constraints (max 3 open positions)
            open_positions = [k for k, v in state["positions"].items() if v == 1]
            if decision["action"] == "enter":
                if tok not in open_positions and len(open_positions) < 3:
                    state["positions"][tok] = 1
                    state["entry_price"][tok] = price
                    state["max_price"][tok] = price
                    state["hold_ticks"][tok] = 0
                    state["trades"] += 1
            elif decision["action"] == "exit":
                if state["positions"].get(tok, 0) == 1:
                    state["positions"][tok] = 0
                    state["pnl"] += price - state["entry_price"].get(tok, price)
                    state["entry_price"][tok] = 0.0
                    state["max_price"][tok] = 0.0
                    state["hold_ticks"][tok] = 0
                    state["cooldown"] = 6
                    state["trades"] += 1

            # Log AG-UI style events for CopilotKit panel visibility
            if decision["action"] in ("enter", "exit"):
                bp = window_stats.get("buy_pressure", 0)
                wc = window_stats.get("whale_concentration", 0)
                cw = window_stats.get("coordinated_wallets", 0)
                ps = decision.get("pump_signal", 0)
                rr = decision.get("rugpull_risk", 0)
                reason_detail = (
                    f"Signals: buy_pressure={bp:.2f}, whale_concentration={wc:.0f}%, "
                    f"coordinated_wallets={cw}, pump_signal={ps}, rugpull_risk={rr}. "
                    f"Momentum={momentum:.3f}, volatility={volatility:.3f}. "
                    f"Position slots open={max(0, 3 - len(open_positions))}."
                )
                append_agui_event(
                    [
                        {
                            "role": "assistant",
                            "content": (
                                f"Agent decided to {decision['action']} {tok} at {price:.2f}. "
                                f"Reason: {decision.get('reason', 'n/a')}. {reason_detail}"
                            ),
                        }
                    ],
                    thread_id="stream",
                )
            elif tick % 25 == 0:
                bp = window_stats.get("buy_pressure", 0)
                wc = window_stats.get("whale_concentration", 0)
                cw = window_stats.get("coordinated_wallets", 0)
                ps = decision.get("pump_signal", 0)
                rr = decision.get("rugpull_risk", 0)
                append_agui_event(
                    [
                        {
                            "role": "assistant",
                            "content": (
                                f"Holding {tok}. Signals: buy_pressure={bp:.2f}, "
                                f"whale_concentration={wc:.0f}%, coordinated_wallets={cw}, "
                                f"pump_signal={ps}, rugpull_risk={rr}. "
                                f"Momentum={momentum:.3f}, volatility={volatility:.3f}."
                            ),
                        }
                    ],
                    thread_id="stream",
                )

            emit_trader_metrics(tok, state)

            # Persist stream trades to Neo4j for graph demo
            if driver:
                write_trades(driver, [t])

            # Coordination edges: consecutive coordinated wallets on same token
            if driver and t.get("coordinated") and last_wallet_by_token.get(tok):
                prev_wallet = last_wallet_by_token[tok]
                if prev_wallet != t["wallet"]:
                    write_coordination_edge(
                        driver,
                        prev_wallet,
                        t["wallet"],
                        tok,
                        "consecutive coordinated buys/sells on same token",
                    )

            # Influence edges: whale buy followed by retail buy within short window
            if t["wallet"].startswith("WHALE") and t["side"] == "buy":
                recent_whale_by_token[tok] = {"wallet": t["wallet"], "tick": tick}
            elif driver and t["wallet"].startswith("RET") and t["side"] == "buy":
                whale = recent_whale_by_token.get(tok)
                if whale and tick - whale["tick"] <= 5:
                    write_influence_edge(
                        driver,
                        whale["wallet"],
                        t["wallet"],
                        tok,
                        "whale buy followed by retail buy",
                    )

            last_wallet_by_token[tok] = t["wallet"]

            global LAST_METRICS, LAST_DECISION
            LAST_METRICS = {
                "token": tok,
                "price": price,
                "window_stats": window_stats,
                "decision": decision,
                "trader": {
                    "positions": state["positions"],
                    "pnl": state["pnl"],
                    "trades": state["trades"],
                },
            }
            if llm_decision:
                LAST_DECISION = llm_decision
            elif not USE_LLM:
                LAST_DECISION = {
                    "action": decision.get("action"),
                    "reason": "Decision based on live signals.",
                }

            # update per-token hold ticks and max price
            if state["positions"].get(tok, 0) == 1:
                state["hold_ticks"][tok] = state["hold_ticks"].get(tok, 0) + 1
                state["max_price"][tok] = max(state["max_price"].get(tok, price), price)

            top_tokens = sorted(recent_counts.items(), key=lambda x: x[1], reverse=True)[:10]

            payload = {
                "trade": t,
                "price": price,
                "window_stats": window_stats,
                "decision": decision,
                "llm_decision": llm_decision,
                "top_tokens": top_tokens,
                "trader": state,
            }
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(speed_ms / 1000.0)
        if driver:
            driver.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream")
