"use client";

import { useEffect, useRef, useState } from "react";
import { CopilotSidebar } from "@copilotkit/react-ui";
import { useCopilotReadable, useCopilotAction } from "@copilotkit/react-core";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

export default function Page() {
  const [market, setMarket] = useState<string>("No market data.");
  const [liveFeed, setLiveFeed] = useState<string>("No live feed yet.");
  const [position, setPosition] = useState<string>("flat");
  const [pnl, setPnl] = useState<number>(0);
  const [series, setSeries] = useState<Record<string, number[]>>({});
  const [posSeries, setPosSeries] = useState<Record<string, number[]>>({});
  const [topTokens, setTopTokens] = useState<string[]>([]);
  const [graphEdges, setGraphEdges] = useState<string[]>([]);
  const [neo4jSummary, setNeo4jSummary] = useState<string>("Not loaded.");
  const [neo4jInsights, setNeo4jInsights] = useState<string>("Not loaded.");
  const [metrics, setMetrics] = useState<string>("No metrics yet.");
  const [aguiEvents, setAguiEvents] = useState<string>("No AG-UI events yet.");
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  function parseJsonSafe(raw: string) {
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  useCopilotReadable({
    description: "Latest WhaleGraph live feed JSON",
    value: liveFeed,
  });

  useCopilotAction({
    name: "get_latest_metrics",
    description: "Get latest stream metrics and agent decision.",
    parameters: [],
    handler: async () => {
      const res = await fetch(`${API_URL}/api/agent/metrics`);
      const data = await res.json();
      return JSON.stringify(data, null, 2);
    },
  });

  async function loadAguiEvents() {
    const res = await fetch(`${API_URL}/api/agui/events`);
    const data = await res.json();
    setAguiEvents(JSON.stringify(data, null, 2));
  }

  async function loadNeo4jSummary() {
    const res = await fetch(`${API_URL}/api/neo4j/summary`);
    const data = await res.json();
    setNeo4jSummary(JSON.stringify(data, null, 2));
  }

  async function loadNeo4jInsights() {
    const res = await fetch(`${API_URL}/api/neo4j/insights`);
    const data = await res.json();
    setNeo4jInsights(JSON.stringify(data, null, 2));
  }

  async function loadMarketSnapshot() {
    const res = await fetch(`${API_URL}/api/market/SOL`);
    const data = await res.json();
    setMarket(JSON.stringify(data, null, 2));
  }

  function startStream() {
    const es = new EventSource(
      `${API_URL}/api/stream?token=SOL&scenario=pump_and_dump&speed_ms=250&dataset=test`
    );
    es.onmessage = (evt) => {
      const raw = String(evt.data || "");
      let data: any = null;
      try {
        data = JSON.parse(raw);
      } catch {
        const cleaned = raw.replace(/^data:\s*/gm, "").trim();
        try {
          data = JSON.parse(cleaned);
        } catch {
          const lastJsonLine = cleaned
            .split("\n")
            .map((l) => l.trim())
            .reverse()
            .find((l) => l.startsWith("{") || l.startsWith("["));
          if (lastJsonLine) {
            try {
              data = JSON.parse(lastJsonLine);
            } catch {
              return;
            }
          } else {
            return;
          }
        }
      }
      setLiveFeed(JSON.stringify(data, null, 2));
      const openPositions = Object.values(data?.trader?.positions || {}).filter(
        (v: any) => v === 1
      ).length;
      setPosition(openPositions > 0 ? `long(${openPositions})` : "flat");
      setPnl(Number((data?.trader?.pnl || 0).toFixed(2)));
      setGraphEdges((prev) => {
        const edge = `${data?.trade?.wallet} → ${data?.trade?.token} (${data?.trade?.side} ${data?.trade?.amount})`;
        const next = [edge, ...prev];
        return next.slice(0, 20);
      });
      const obs = {
        token: data?.trade?.token,
        price: data?.price,
        buy_pressure: data?.window_stats?.buy_pressure,
        whale_concentration: data?.window_stats?.whale_concentration,
        coordinated_wallets: data?.window_stats?.coordinated_wallets,
        pump_signal: data?.decision?.pump_signal,
        rugpull_risk: data?.decision?.rugpull_risk,
        momentum: data?.decision?.momentum,
        volatility: data?.decision?.volatility,
        open_positions: Object.values(data?.trader?.positions || {}).filter(
          (v: any) => v === 1
        ).length,
        pnl: data?.trader?.pnl,
        trades: data?.trader?.trades,
      };
      setMetrics(JSON.stringify(obs, null, 2));
      const tok = data?.trade?.token || "UNK";
      setSeries((prev) => {
        const next = { ...prev };
        const arr = next[tok] ? [...next[tok]] : [];
        arr.push(data?.price || 0);
        next[tok] = arr.slice(-200);
        return next;
      });
      setPosSeries((prev) => {
        const next = { ...prev };
        const arr = next[tok] ? [...next[tok]] : [];
        const pos = data?.trader?.positions?.[tok] || 0;
        arr.push(pos);
        next[tok] = arr.slice(-200);
        return next;
      });
      const tt = (data?.top_tokens || []).map((t: any) => t[0]).slice(0, 10);
      if (tt.length > 0) setTopTokens(tt);
    };
    es.onerror = () => {
      es.close();
    };
  }

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || topTokens.length === 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0f151b";
    ctx.fillRect(0, 0, w, h);
    const colors = ["#4fd1c5", "#f6c453", "#7bdff2", "#f08a5d", "#b83b5e", "#6a2c70", "#8ac926", "#1982c4", "#ff595e", "#ffca3a"];

    // build global min/max across top tokens
    const allPrices: number[] = [];
    for (const tok of topTokens) {
      const arr = series[tok] || [];
      allPrices.push(...arr);
    }
    if (allPrices.length < 2) return;
    const min = Math.min(...allPrices);
    const max = Math.max(...allPrices);
    const span = Math.max(0.01, max - min);

    const toY = (p: number) => h - 10 - ((p - min) / span) * (h - 20);

    topTokens.forEach((tok, idx) => {
      const prices = series[tok] || [];
      const positions = posSeries[tok] || [];
      if (prices.length < 2) return;
      const toX = (i: number) => (i / (prices.length - 1)) * (w - 20) + 10;
      for (let i = 1; i < prices.length; i++) {
        const x1 = toX(i - 1);
        const y1 = toY(prices[i - 1]);
        const x2 = toX(i);
        const y2 = toY(prices[i]);
        const pos = positions[i] || 0;
        ctx.strokeStyle = pos === 1 ? colors[idx % colors.length] : "#9bb0c8";
        ctx.lineWidth = pos === 1 ? 2.5 : 1.5;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
    });
  }, [series, posSeries, topTokens]);

  useEffect(() => {
    loadAguiEvents().catch(() => {});
    loadNeo4jSummary().catch(() => {});
    loadNeo4jInsights().catch(() => {});
    loadMarketSnapshot().catch(() => {});
    const id = setInterval(() => {
      loadAguiEvents().catch(() => {});
      loadNeo4jSummary().catch(() => {});
      loadNeo4jInsights().catch(() => {});
    }, 4000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    startStream();
  }, []);

  const live = parseJsonSafe(liveFeed);
  const metricsObj = parseJsonSafe(metrics);
  const aguiObj = parseJsonSafe(aguiEvents);
  const neo4jSummaryObj = parseJsonSafe(neo4jSummary);
  const neo4jInsightsObj = parseJsonSafe(neo4jInsights);
  const marketObj = parseJsonSafe(market);

  return (
    <div className="wrap">
      <header className="hero">
        <div>
          <div className="eyebrow">On-Chain Behavioral Risk Intelligence</div>
          <h1>WhaleGraph AI</h1>
          <p className="subhead">
            Agentic market intelligence: live stream, graph modeling, LLM
            reasoning, and observability.
          </p>
        </div>
        <div className="hero__stats">
          <div className="stat">
            <span className="stat__label">Position</span>
            <span className="stat__value">{position}</span>
          </div>
          <div className="stat">
            <span className="stat__label">PnL</span>
            <span className="stat__value">${pnl.toFixed(2)}</span>
          </div>
          <div className="stat">
            <span className="stat__label">Top Tokens</span>
            <span className="stat__value">
              {topTokens.slice(0, 5).join(", ") || "—"}
            </span>
          </div>
        </div>
      </header>

      <div className="dashboard">
        <section className="panel span-8">
          <div className="panel__title">Live Stream (Paper Trader)</div>
          <div className="chart-wrap">
            <canvas ref={canvasRef} width={900} height={260} />
          </div>
          <div className="panel__subtitle">Latest Event</div>
          {live ? (
            <div className="kv">
              <div className="kv__row">
                <span className="kv__label">Token</span>
                <span className="kv__value">{live?.trade?.token || "—"}</span>
              </div>
              <div className="kv__row">
                <span className="kv__label">Side</span>
                <span className="kv__value">{live?.trade?.side || "—"}</span>
              </div>
              <div className="kv__row">
                <span className="kv__label">Amount</span>
                <span className="kv__value">{live?.trade?.amount ?? "—"}</span>
              </div>
              <div className="kv__row">
                <span className="kv__label">Price</span>
                <span className="kv__value">{live?.price?.toFixed?.(2) ?? live?.price ?? "—"}</span>
              </div>
              <div className="kv__row">
                <span className="kv__label">Decision</span>
                <span className="kv__value">{live?.decision?.action || "—"}</span>
              </div>
              <div className="kv__row">
                <span className="kv__label">Reason</span>
                <span className="kv__value">{live?.decision?.reason || "—"}</span>
              </div>
            </div>
          ) : (
            <pre className="code">{liveFeed}</pre>
          )}
        </section>

        <section className="panel span-4">
          <div className="panel__title">Observability Metrics</div>
          {metricsObj ? (
            <div className="metrics-grid">
              {[
                ["Token", metricsObj.token],
                ["Price", metricsObj.price?.toFixed?.(2) ?? metricsObj.price],
                ["Buy Pressure", metricsObj.buy_pressure],
                ["Whale %", metricsObj.whale_concentration],
                ["Coord Wallets", metricsObj.coordinated_wallets],
                ["Pump Signal", metricsObj.pump_signal],
                ["Rugpull Risk", metricsObj.rugpull_risk],
                ["Momentum", metricsObj.momentum],
                ["Volatility", metricsObj.volatility],
                ["Open Positions", metricsObj.open_positions],
                ["PnL", metricsObj.pnl],
                ["Trades", metricsObj.trades],
              ].map(([label, value]) => (
                <div className="metric" key={label}>
                  <div className="metric__label">{label}</div>
                  <div className="metric__value">
                    {value === undefined ? "—" : String(value)}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <pre className="code">{metrics}</pre>
          )}
        </section>

        <section className="panel span-4">
          <div className="panel__title">AG-UI Events</div>
          {aguiObj?.events ? (
            <div className="list">
              {aguiObj.events.slice(0, 6).map((e: any, idx: number) => (
                <div className="list__item" key={`${e.run_id}-${idx}`}>
                  <div className="list__title">
                    {new Date(e.ts).toLocaleTimeString()}
                  </div>
                  <div className="list__body">
                    {e?.messages?.[0]?.content || "—"}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <pre className="code">{aguiEvents}</pre>
          )}
        </section>

        <section className="panel span-4">
          <div className="panel__title">Graph History</div>
          <div className="panel__subtitle">
            Edge legend: wallet → token (trade). Coordinated wallets create
            coordination edges; whale→retail edges indicate influence.
          </div>
          <div className="mini-graph">
            {(() => {
              const edges = graphEdges
                .slice(0, 12)
                .map((e) => e.split(" → "))
                .map(([a, rest]) => {
                  const b = (rest || "").split(" ")[0];
                  return { a: a?.trim(), b: b?.trim() };
                })
                .filter((e) => e.a && e.b);
              const nodes = Array.from(
                new Set(edges.flatMap((e) => [e.a, e.b]))
              );
              const size = 220;
              const center = size / 2;
              const radius = 80;
              const positions = new Map<string, { x: number; y: number }>();
              nodes.forEach((n, i) => {
                const angle = (i / Math.max(1, nodes.length)) * Math.PI * 2;
                positions.set(n, {
                  x: center + Math.cos(angle) * radius,
                  y: center + Math.sin(angle) * radius,
                });
              });
              return (
                <svg viewBox={`0 0 ${size} ${size}`}>
                  {edges.map((e, i) => {
                    const s = positions.get(e.a)!;
                    const t = positions.get(e.b)!;
                    return (
                      <line
                        key={`l-${i}`}
                        x1={s.x}
                        y1={s.y}
                        x2={t.x}
                        y2={t.y}
                        stroke="rgba(79, 209, 197, 0.5)"
                        strokeWidth="1.2"
                      />
                    );
                  })}
                  {nodes.map((n) => {
                    const p = positions.get(n)!;
                    return (
                      <g key={n}>
                        <circle
                          cx={p.x}
                          cy={p.y}
                          r={6}
                          fill="#4cc9f0"
                        />
                        <text x={p.x + 8} y={p.y + 4} fontSize="9" fill="#9aa7b5">
                          {n}
                        </text>
                      </g>
                    );
                  })}
                </svg>
              );
            })()}
          </div>
          <div className="panel__subtitle">Latest edges</div>
          <pre className="code">{graphEdges.join("\n")}</pre>
        </section>

        <section className="panel span-6">
          <div className="panel__title">Neo4j Graph Summary</div>
          {neo4jSummaryObj ? (
            <div className="kv">
              {Object.entries(neo4jSummaryObj).map(([k, v]) => (
                <div className="kv__row" key={k}>
                  <span className="kv__label">{k}</span>
                  <span className="kv__value">{String(v)}</span>
                </div>
              ))}
            </div>
          ) : (
            <pre className="code">{neo4jSummary}</pre>
          )}
        </section>

        <section className="panel span-6">
          <div className="panel__title">Neo4j Insights</div>
          {neo4jInsightsObj ? (
            <div className="list">
              {Object.entries(neo4jInsightsObj).map(([k, v]) => (
                <div className="list__item" key={k}>
                  <div className="list__title">{k}</div>
                  <div className="list__body">{String(v)}</div>
                </div>
              ))}
            </div>
          ) : (
            <pre className="code">{neo4jInsights}</pre>
          )}
        </section>

        <section className="panel span-4">
          <div className="panel__title">Market Snapshot</div>
          {marketObj ? (
            <div className="kv">
              {Object.entries(marketObj).map(([k, v]) => (
                <div className="kv__row" key={k}>
                  <span className="kv__label">{k}</span>
                  <span className="kv__value">{String(v)}</span>
                </div>
              ))}
            </div>
          ) : (
            <pre className="code">{market}</pre>
          )}
        </section>
      </div>

      <CopilotSidebar
        instructions={
          "You are WhaleGraph Copilot. Summarize risk signals for the current token and explain why pump or rugpull risk is rising."
        }
        defaultOpen
        labels={{
          title: "WhaleGraph Copilot",
          initial: "Ask me why the token looks risky.",
        }}
      />
    </div>
  );
}
