import os

from google.adk.agents import Agent, LoopAgent, SequentialAgent
from google.adk.code_executors import UnsafeLocalCodeExecutor
from google.adk.tools import exit_loop, request_input
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

from .tools import get_news_sentiment, log_dry_run_trade

# Gemini-only — a real-money agent stays on one well-understood provider
# rather than the multi-provider switch used in data_engineering_agent.
MODEL_FLASH = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
MODEL_PRO = os.getenv("GEMINI_PRO_MODEL", "gemini-pro-latest")

# Hard-coded risk limits, overridable via .env — see README.md "Risk config".
MAX_CAPITAL_PER_TRADE_INR = int(os.getenv("MAX_CAPITAL_PER_TRADE_INR", "10000"))
MAX_DAILY_LOSS_INR = int(os.getenv("MAX_DAILY_LOSS_INR", "2000"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
MIN_DAYS_TO_EXPIRY = int(os.getenv("MIN_DAYS_TO_EXPIRY", "2"))

# Kite MCP is a remote server reached via mcp-remote (same command as the
# global Claude Code config in ~/.claude.json) — ADK needs its own McpToolset
# wiring, it does not share that config. tool_filter is fixed per McpToolset
# instance, so each pipeline stage below gets its own instance built from
# this shared connection_params, scoped to only the tools that stage needs.
# This means place_order/modify_order/cancel_order are structurally
# unreachable from every agent except order_execution_agent, regardless of
# what the LLM decides to do.
# timeout is generous because the first connection may pop a browser for the
# Kite OAuth login flow (mcp-remote caches the token after that, typically in
# ~/.mcp-auth, so subsequent connections are fast).
KITE_MCP_PARAMS = StdioConnectionParams(
    server_params=StdioServerParameters(
        command="npx",
        args=["mcp-remote", "https://mcp.kite.trade/mcp"],
    ),
    timeout=60.0,
)

market_data_toolset = McpToolset(
    connection_params=KITE_MCP_PARAMS,
    tool_filter=["search_instruments", "get_quotes", "get_ltp", "get_ohlc", "get_historical_data"],
)
account_toolset = McpToolset(
    connection_params=KITE_MCP_PARAMS,
    tool_filter=["login", "get_profile", "get_positions", "get_holdings", "get_margins", "get_orders", "get_gtts"],
)
execution_toolset = McpToolset(
    connection_params=KITE_MCP_PARAMS,
    tool_filter=["place_order", "modify_order", "cancel_order", "get_order_history"],
)

# ── STAGE 1: Mode + watchlist ────────────────────────────────────────────────

trading_mode_chooser = Agent(
    name="TradingModeChooser",
    model=MODEL_FLASH,
    description="Establishes dry-run vs. live mode for this session and confirms the watchlist.",
    tools=[request_input],
    instruction="""
You are the safety checkpoint at the start of every trading session.

1. Read the human's opening message for the instruments/watchlist they want analysed
   (equities, and/or F&O like index options or stock futures). If none was given, call
   request_input() to ask for one — do not assume a default watchlist.
2. Call request_input() to ask: "Should this session run in 'dry_run' mode (no real orders,
   everything simulated and logged) or 'live' mode (approved orders are placed for real with
   real money)? Type 'dry_run' or 'live'."
3. If the answer is ambiguous, call request_input() again to clarify — never guess, this
   controls whether real money moves.
4. Record your decision as exactly one of these lines, plus the confirmed watchlist:
   - "TRADING MODE: dry_run"
   - "TRADING MODE: live"
   Followed by: "WATCHLIST: <comma-separated instruments>"
""",
)

# ── STAGE 2: Market data + technicals ────────────────────────────────────────

market_data_fetcher = Agent(
    name="MarketDataFetcher",
    model=MODEL_FLASH,
    description="Resolves instruments and pulls quotes + historical OHLC data from Kite.",
    tools=[market_data_toolset],
    instruction="""
You are a market data specialist. Read the WATCHLIST recorded by TradingModeChooser.

For each instrument:
1. Call search_instruments() to resolve the correct tradingsymbol/exchange (and for F&O,
   confirm available expiries and strikes near the current price).
2. Call get_quotes() or get_ltp() for the current price.
3. Call get_historical_data() for a suitable lookback (at least 60 trading days for equity,
   shorter for near-expiry F&O) at daily interval, plus get_ohlc() for the current session.

Print the resolved instrument details and the full historical OHLC series (dates, open, high,
low, close, volume) as clearly labelled data per instrument — this is read by
TechnicalIndicatorAnalyst next, which does not have tool access and depends entirely on what
you print here. Do not summarize away the raw numbers.
""",
)

technical_indicator_analyst = Agent(
    name="TechnicalIndicatorAnalyst",
    model=MODEL_FLASH,
    description="Computes technical indicators from the fetched OHLC data to characterise trend.",
    code_executor=UnsafeLocalCodeExecutor(),
    instruction="""
You are a quantitative analyst. Read the OHLC data MarketDataFetcher printed earlier in this
conversation (you have no tools — you must copy the numbers you were given into your code as
literal data, you cannot re-fetch them).

Write and run Python (use polars) in a single ```python fenced block that, per instrument:
- Computes SMA20, SMA50, RSI14, MACD (12/26/9), and ATR14.
- Classifies trend as uptrend/downtrend/sideways from the moving averages and price position.
- Classifies momentum strength from RSI/MACD.
- Reports ATR-based volatility, useful for sizing stop-loss distance.
- print() a compact structured signal summary per instrument (trend, strength, volatility,
  the key indicator values).

If the OHLC history for an instrument is too short for a given indicator's lookback, compute
what is possible and note the limitation instead of failing.

Output: the executable code block, then a one-paragraph plain-English summary of the printed
signals per instrument.
""",
)

# ── STAGE 3: News/sentiment ──────────────────────────────────────────────────

news_sentiment_analyst = Agent(
    name="NewsSentimentAnalyst",
    model=MODEL_FLASH,
    description="Fetches and summarises recent news sentiment for each watchlisted instrument.",
    tools=[get_news_sentiment],
    instruction="""
You are a markets news analyst. Read the WATCHLIST from TradingModeChooser.

For each instrument, call get_news_sentiment() with the company/instrument name (for index
derivatives, search the underlying index name too, e.g. "NIFTY 50"). Summarise, per instrument:
- Overall sentiment: bullish / bearish / neutral / mixed.
- Any high-impact flagged events (earnings, regulatory action, RBI policy, budget, geopolitical
  news, major management commentary) that could override a purely technical read.

If get_news_sentiment() reports the API key is missing or the request failed, say so plainly
and note that this instrument's decision will rely on technicals only — do not fabricate news.
""",
)

# ── STAGE 4: Account/portfolio context ───────────────────────────────────────

position_risk_context = Agent(
    name="PositionRiskContext",
    model=MODEL_FLASH,
    description="Confirms Kite auth, then pulls current positions, holdings, margins, and GTTs.",
    tools=[account_toolset],
    instruction="""
You are the account-state checkpoint. Do this in order:

1. Call get_profile() as a connectivity/auth check.
2. If get_profile() fails or indicates there is no active session, call login(), then present
   the returned URL/instructions clearly to the human, and stop here — output exactly:
   "SESSION BLOCKED: Kite login required. Complete login at the link above and re-run this
   agent." Do not proceed to the remaining steps or invent account data.
3. Otherwise, call get_positions(), get_holdings(), get_margins(), get_orders(), and get_gtts().
4. Summarise: open positions (with unrealised P&L), holdings, available cash margin, available
   F&O margin (SPAN + exposure), today's realised P&L if visible from orders/positions, count
   of currently open positions, and any active GTTs relevant to the watchlist.

This summary is the ground truth RiskGate will use for numeric checks later — be precise with
numbers, do not round away important figures.
""",
)

# ── STAGE 5: Decide → risk-check → human approval (loops up to 3x) ──────────

strategy_decision_agent = Agent(
    name="StrategyDecisionAgent",
    model=MODEL_PRO,
    description="Synthesises technicals, news, and account context into a concrete order proposal or NO ACTION.",
    instruction=f"""
If PositionRiskContext's output contains "SESSION BLOCKED": immediately output "NO ACTION —
Kite session not authenticated, see PositionRiskContext's message above." and stop. Do not
analyse further or propose an order without real account data.

You are a senior discretionary trader. Synthesise, from earlier in this conversation:
- TechnicalIndicatorAnalyst's trend/momentum/volatility signals.
- NewsSentimentAnalyst's sentiment summary.
- PositionRiskContext's account state (positions, margin, open position count).

If this is a retry iteration, read RiskGate's last "RISK CHECK: FAIL" reasons or the human's
"modify" feedback from HumanApprovalGate, and revise your previous proposal accordingly instead
of starting over.

Decide one of:
- "NO ACTION" — if no instrument presents a sufficiently clear, risk-appropriate setup. Give a
  one-paragraph reason. This ends the cycle.
- A single concrete order proposal (only one order per cycle) with ALL of these fields spelled
  out explicitly:
  - Exchange (NSE/BSE/NFO/BFO), tradingsymbol (exact, including expiry/strike for F&O)
  - Transaction type: BUY or SELL
  - Quantity (respect exchange lot size for F&O)
  - Product: CNC (equity delivery), MIS (intraday), or NRML (F&O carry)
  - Order type: MARKET, LIMIT, SL, or SL-M, with price/trigger_price as applicable
  - Mandatory stop-loss level and target level (required for every proposal, especially F&O)
  - Rationale: 2-3 sentences citing the specific technical and news signals that drove this

Stay within a sane order of magnitude for {MAX_CAPITAL_PER_TRADE_INR} INR of capital per trade
given current margin — RiskGate will verify this precisely, but do not propose something wildly
outside it.

Output must clearly start with either "NO ACTION" or "ORDER PROPOSAL:" followed by the fields
above, one per line.
""",
)

risk_gate = Agent(
    name="RiskGate",
    model=MODEL_FLASH,
    description="Numerically validates the order proposal against hard risk limits before it can reach the human.",
    code_executor=UnsafeLocalCodeExecutor(),
    tools=[exit_loop],
    instruction=f"""
You are the automated risk desk. Read StrategyDecisionAgent's latest output.

If it was "NO ACTION": output "RISK CHECK: N/A — no order proposed." then call exit_loop()
immediately. Nothing else to do.

Otherwise, an ORDER PROPOSAL was made. Read PositionRiskContext's account summary from earlier
in this conversation (available margin, open position count, today's realised P&L) and write
Python code in a ```python fenced block that computes, using the literal numbers from that
summary and the proposal (copy the numbers in — you have no tools):

1. order_value = quantity * price (or a reasonable estimate if MARKET order — use LTP from
   MarketDataFetcher's data)
2. order_value <= {MAX_CAPITAL_PER_TRADE_INR} (MAX_CAPITAL_PER_TRADE_INR)
3. order_value <= available margin for the relevant segment (equity cash vs F&O SPAN+exposure)
4. current open position count < {MAX_OPEN_POSITIONS} (MAX_OPEN_POSITIONS) OR this order
   reduces/exits an existing position
5. today's realised loss so far, if this were to also lose, would not exceed
   {MAX_DAILY_LOSS_INR} INR (MAX_DAILY_LOSS_INR) — be conservative, use the stop-loss distance
   to estimate worst case
6. for any NFO/BFO instrument: a stop-loss level was specified in the proposal (mandatory), AND
   days-to-expiry >= {MIN_DAYS_TO_EXPIRY} (MIN_DAYS_TO_EXPIRY)

print() each check with PASS/FAIL and the numbers used.

After the code block, if every check passed, output exactly: "RISK CHECK: PASS" and do NOT call
exit_loop() — a human still needs to approve this. If any check failed, output exactly:
"RISK CHECK: FAIL — <one line per failed check with the specific numbers>" and do NOT call
exit_loop() — StrategyDecisionAgent gets another attempt next iteration.
""",
)

human_approval_gate = Agent(
    name="HumanApprovalGate",
    model=MODEL_FLASH,
    description="The mandatory human checkpoint — every single order must be explicitly approved here before execution.",
    tools=[request_input, exit_loop],
    instruction="""
You are the mandatory human-in-the-loop checkpoint. NO order may execute without your explicit
human approval recorded here — there are no exceptions or thresholds.

1. If RiskGate's last output was "RISK CHECK: N/A" (no proposal) — nothing to do, output
   "Nothing to approve this cycle." and call exit_loop().
2. If RiskGate's last output was "RISK CHECK: FAIL ..." — do NOT ask the human. Output
   "Risk check failed, not sending to approval. Will retry with a revised proposal if attempts
   remain." and do NOT call exit_loop() (let the loop retry).
3. If RiskGate's last output was "RISK CHECK: PASS" — present the FULL order proposal (every
   field from StrategyDecisionAgent), its rationale, and the risk-check numbers, as a clear
   ticket. Call request_input() to ask: "Approve this order? Type 'approve' to proceed, 'reject'
   to discard it, or describe what should change to have it revised."
   - If they approve: record "ORDER DECISION: approve" and call exit_loop().
   - If they reject: record "ORDER DECISION: reject" and call exit_loop().
   - If they give modification feedback: record "Human feedback: <their feedback>" for
     StrategyDecisionAgent to read next iteration, and do NOT call exit_loop().
""",
)

decision_loop = LoopAgent(
    name="DecisionLoop",
    description=(
        "Proposes an order, numerically risk-checks it, and requires explicit human approval "
        "— retries up to 3 times if the risk check fails or the human asks for changes."
    ),
    sub_agents=[strategy_decision_agent, risk_gate, human_approval_gate],
    max_iterations=3,
)

# ── STAGE 6: Execution ────────────────────────────────────────────────────────

order_execution_agent = Agent(
    name="OrderExecutionAgent",
    model=MODEL_FLASH,
    description=(
        "Executes the approved order — as a real Kite order in live mode, or a logged "
        "simulation in dry-run mode. Never places an order without an explicit approval."
    ),
    tools=[execution_toolset, log_dry_run_trade],
    instruction="""
You are the execution desk. Read TRADING MODE (from TradingModeChooser) and ORDER DECISION
(from HumanApprovalGate) from earlier in this conversation.

- If there is no ORDER DECISION marker anywhere in the conversation (the decision loop ran out
  of its 3 attempts without the human resolving it), treat this as an implicit reject: take no
  action and output "No order decision was reached within the allowed attempts — no action
  taken. Re-run the session to try again."
- If the cycle ended with "NO ACTION" or "ORDER DECISION: reject": take no action. Output a
  short confirmation that nothing was executed.
- If "ORDER DECISION: approve" and "TRADING MODE: dry_run": do NOT call any Kite order tool.
  Call log_dry_run_trade() with a concise order_summary (all fields from the approved proposal)
  and the rationale. Report it as a simulated fill, quoting the ledger confirmation.
- If "ORDER DECISION: approve" and "TRADING MODE: live": call place_order() with the exact
  approved parameters, then call get_order_history() to confirm the resulting status. Report the
  real order ID and status. If place_order() fails, report the exact error — do not retry
  silently or guess at a fix.
""",
)

# ── Root ──────────────────────────────────────────────────────────────────────
# NOTE: LoopAgent is deprecated in google-adk 2.x in favour of the graph-based
# Workflow API (which cannot yet be used as an LlmAgent sub-agent). It remains
# functional in 2.4.0 and is already used the same way in data_engineering_agent.
root_agent = SequentialAgent(
    name="TradingOrchestrator",
    description=(
        "Kite-connected trading agent: analyses technicals + news for a human-given watchlist, "
        "proposes at most one order per session, numerically risk-checks it, and requires "
        "explicit human approval before ever executing anything. Defaults to dry-run; live "
        "order placement requires an explicit per-session opt-in."
    ),
    sub_agents=[
        trading_mode_chooser,
        market_data_fetcher,
        technical_indicator_analyst,
        news_sentiment_analyst,
        position_risk_context,
        decision_loop,
        order_execution_agent,
    ],
)
