# Trade Agent

A Kite-connected trading agent that reads market technicals and news to form a view on
trend, proposes **at most one order per session**, numerically risk-checks it, and requires
**explicit human approval before it can ever execute anything** — real money is only ever
moved after a human has typed "approve" to a specific order ticket.

> ⚠️ **This agent can place real orders with real money.** It defaults to `dry_run` mode
> every session — read [Safety model](#safety-model) before ever choosing `live`.

---

## Safety model

Three independent layers, so no single mistake (prompt injection, model error, bad instinct)
can cause an unapproved trade:

1. **Tool-level isolation.** The Kite MCP server exposes 20+ tools, including
   `place_order`/`modify_order`/`cancel_order`. `agent.py` connects to it through **three
   separate `McpToolset` instances**, each with a fixed `tool_filter`. Only
   `OrderExecutionAgent` is ever given the order-placement tools — every other agent
   (market data, news, decision-making, risk-checking) is structurally incapable of calling
   them, regardless of what the LLM decides. See `market_data_toolset` / `account_toolset` /
   `execution_toolset` in `agent.py`.
2. **Mandatory human approval, every order, no exceptions.** `HumanApprovalGate` uses
   `request_input()` to show the full order ticket (instrument, side, quantity, price,
   stop-loss, target, rationale, risk-check results) and waits for an explicit
   `approve`/`reject`/modify response. There is no capital threshold or order type that
   skips this — see `DecisionLoop` in `agent.py`.
3. **Dry-run by default.** `TradingModeChooser` asks once per session for `dry_run` or
   `live`. In `dry_run`, `OrderExecutionAgent` never calls a real Kite order tool — it logs
   the would-be order to `trade_agent/ledger/dry_run_trades.jsonl` (gitignored) instead. Real
   orders only fire when the session was explicitly started in `live` mode **and** the
   specific order was approved.

On top of that, `RiskGate` numerically validates every proposal (in real executed Python, not
LLM eyeballing) against hard-coded limits before it's even shown to the human — see
[Risk config](#risk-config).

---

## Architecture

```
SequentialAgent  (TradingOrchestrator)  ← root_agent
│  One full analyse → decide → approve → execute cycle per conversation turn.
│  This is an interactive agent, not an unattended/cron loop — a human must be
│  present to approve orders.
│
├── TradingModeChooser              [tools: request_input]
│     Confirms watchlist from the human's opening message. Asks dry_run vs.
│     live. Records "TRADING MODE: ..." / "WATCHLIST: ...".
│
├── MarketDataFetcher                [tools: market_data_toolset]
│     search_instruments / get_quotes / get_ltp / get_ohlc / get_historical_data.
│     Prints raw OHLC series — has no code executor, just tool calls.
│
├── TechnicalIndicatorAnalyst        [code_executor: UnsafeLocalCodeExecutor]
│     Reads the printed OHLC data, computes SMA/RSI/MACD/ATR in real executed
│     polars code, prints a structured trend/strength/volatility signal.
│
├── NewsSentimentAnalyst             [tools: get_news_sentiment]
│     Custom tool (trade_agent/tools.py) hitting a dedicated news API.
│     Summarises bullish/bearish/neutral sentiment + high-impact events.
│
├── PositionRiskContext              [tools: account_toolset]
│     get_profile as an auth check first (calls login + halts the cycle with
│     "SESSION BLOCKED" if not authenticated). Otherwise pulls positions,
│     holdings, margins, orders, GTTs — the ground truth RiskGate uses.
│
├── LoopAgent  (DecisionLoop, max 3 rounds)
│   │
│   ├── StrategyDecisionAgent  [model: gemini-pro-latest, no tools]
│   │     Synthesises technicals + news + account state → "NO ACTION" or one
│   │     concrete ORDER PROPOSAL (instrument, side, qty, product, order type,
│   │     price, mandatory SL/target, rationale).
│   │
│   ├── RiskGate  [code_executor: UnsafeLocalCodeExecutor, tools: exit_loop]
│   │     Computes (not eyeballs) order value vs. MAX_CAPITAL_PER_TRADE_INR,
│   │     available margin, MAX_OPEN_POSITIONS, MAX_DAILY_LOSS_INR, and for
│   │     F&O: mandatory stop-loss + MIN_DAYS_TO_EXPIRY. Records PASS/FAIL.
│   │     exit_loop() only on "NO ACTION" (nothing to check or approve).
│   │
│   └── HumanApprovalGate  [tools: request_input, exit_loop]   ← the HITL gate
│         FAIL → reports it, loop retries (StrategyDecisionAgent revises).
│         PASS → shows the full ticket, calls request_input().
│         approve/reject → records "ORDER DECISION: ..." → exit_loop().
│         modify feedback → recorded for next iteration → loop retries.
│
└── OrderExecutionAgent              [tools: execution_toolset, log_dry_run_trade]
      No ORDER DECISION reached in 3 rounds → implicit reject, no action.
      reject / NO ACTION → no action.
      approve + dry_run → log_dry_run_trade() only, never a real Kite call.
      approve + live → place_order() for real, then get_order_history() to
      confirm status.
```

Model split: everything uses `gemini-flash-latest` except `StrategyDecisionAgent`, which uses
`gemini-pro-latest` — synthesising multi-signal, multi-instrument trade reasoning is exactly
the "complex agentic workflow / deep reasoning" case from the repo's model table, while every
other step here is closer to tool-calling, summarisation, or a fixed numeric checklist.

---

## Kite MCP connection

Kite MCP is Zerodha's official **remote** MCP server, reached via `mcp-remote`:

```python
KITE_MCP_PARAMS = StdioConnectionParams(
    server_params=StdioServerParameters(command="npx", args=["mcp-remote", "https://mcp.kite.trade/mcp"]),
    timeout=60.0,
)
```

This is independent of any Claude Code MCP config you may already have for Kite — ADK agents
don't share that, `agent.py` wires its own connection. The first connection may pop a browser
for Kite's OAuth login; `mcp-remote` caches the resulting token (typically under
`~/.mcp-auth`) so subsequent runs don't re-prompt. If `PositionRiskContext` reports **"SESSION
BLOCKED"**, follow the login link it prints and re-run the agent.

---

## How to run

```bash
uv sync                    # installs mcp + requests (added for this agent)
cp .env.example .env       # if you haven't already; fill in GOOGLE_API_KEY at minimum
uv run adk run trade_agent
uv run adk web trade_agent
```

`node`/`npx` must be available on your PATH — `mcp-remote` is fetched and run via `npx` on
first connection.

---

## Example prompts

```
I want to look at RELIANCE and INFY for a possible intraday trade today.

Check NIFTY 50 and give me an options view — I'm open to a weekly ATM call or put
depending on trend, but nothing within 2 days of expiry.

Just check my current positions and tell me if anything needs a stop-loss adjustment,
don't propose any new entries.
```

The agent reads your watchlist straight out of your opening message — there's no hard-coded
default watchlist.

---

## Risk config

All overridable via `.env` (see `.env.example`), enforced numerically by `RiskGate` before
any proposal reaches `HumanApprovalGate`:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_CAPITAL_PER_TRADE_INR` | `10000` | Max order value per proposed trade |
| `MAX_DAILY_LOSS_INR` | `2000` | Max estimated worst-case loss (via stop-loss distance) before a new entry is blocked for the day |
| `MAX_OPEN_POSITIONS` | `5` | Max concurrent open positions before new entries (not exits) are blocked |
| `MIN_DAYS_TO_EXPIRY` | `2` | F&O only — refuses new entries within this many days of expiry |

`NEWS_API_KEY` / `NEWS_API_BASE_URL` configure the news/sentiment source (default assumes a
NewsAPI.org-shaped `/everything` endpoint). Without a key, `NewsSentimentAnalyst` degrades
gracefully and the agent proceeds on technicals only — it never fabricates news.

---

## Key design decisions

- **One order per session, one cycle per conversation turn.** This is deliberately not an
  unattended/cron-driven agent — `HumanApprovalGate` needs a human present to answer
  `request_input()`. Continuous/scheduled *analysis* (not unattended live execution) would be
  a reasonable follow-up via the repo's scheduling tooling, but unattended live order
  placement is out of scope by design, not by omission.
- **Fetching and computing are separate agents** (`MarketDataFetcher` vs.
  `TechnicalIndicatorAnalyst`), even though ADK technically allows one agent to hold both
  `tools=` and a `code_executor=`. `data_engineering_agent` never combines them on one agent
  either — keeping tool-calling and code-execution turns separate is the established, more
  reliable pattern in this repo.
- **Three `McpToolset` instances, not one shared instance with a runtime filter.**
  `tool_filter` is fixed at `McpToolset.__init__` time in this version of `google-adk`
  (2.4.0) — there's no way to reuse one instance with a different filter per agent. Three
  instances sharing one `connection_params` constant gets both correctness and the
  tool-isolation safety property for free.
- **`RiskGate` runs code instead of reasoning in prose.** Money-handling arithmetic (order
  value vs. margin, worst-case loss estimation) is exactly the kind of thing an LLM can get
  subtly wrong in free text; `UnsafeLocalCodeExecutor` forces it to actually compute the
  numbers from the values earlier agents printed.
- **`StrategyDecisionAgent` has zero tools.** It only reasons over what earlier agents already
  printed into the conversation — this keeps the one agent capable of "deciding to trade"
  structurally unable to call anything, Kite or otherwise.
