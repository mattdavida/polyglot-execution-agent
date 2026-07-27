# Monday Morning — SSE Streaming Implementation

> Pick up here. Goal: stream LLM reasoning tokens to the HITL panel in real time as `strategy_node` runs.  
> Estimated: 3–4 hours total across backend, frontend, and wiring.

---

## First 5 Minutes — Validate Streaming Works

Before writing any code, open a Python shell (venv active) and run this:

```python
from backend.tools.llm_client import get_chat_llm

llm = get_chat_llm(temperature=0.2)
for chunk in llm.stream([{"role": "user", "content": "Say hello in 10 words"}]):
    print(chunk.content, end="", flush=True)
print()
```

**Expected:** tokens print one by one as they arrive.  
**If it dumps all at once:** streaming is disabled on the Azure OpenAI deployment. Fix: go to Azure Portal → OpenAI resource → Model deployments → edit the deployment and confirm streaming is enabled. This is a one-line config fix, not a code change.

Everything below assumes streaming works.

---

## The Approach — Two-Phase LLM Call (Option A)

`strategy_node` currently makes one `structured_llm.invoke()` call which returns a validated JSON object. This blocks the whole response — no tokens flow until the full JSON is complete.

**Split it into two sequential calls:**

| Call | Type | Output |
|---|---|---|
| Call 1 | Plain completion, `streaming=True` | 2–3 sentence reasoning — streams token by token |
| Call 2 | `with_structured_output(StrategyOutput)` | `{approach, num_slices, shares_per_slice}` — instant |

Call 2 uses Call 1's reasoning as context. The `reasoning` field in the final `Strategy` is the text from Call 1. No information is lost, and the trader watches the reasoning appear live before the structured fields snap in.

---

## Phase 1 — Backend (~1.5 hours)

### `backend/pipeline/nodes/strategy_node.py`

Add a two-phase call. Call 1 streams reasoning, Call 2 produces the structured output. The node still returns the same `{"strategy": {...}}` dict — nothing downstream changes.

The tricky part: `strategy_node.run()` is currently synchronous. For SSE, you need an **async generator variant** that yields token chunks. Two options:

- **Option A1 (cleaner):** Add a new `async def stream(state)` method alongside the existing `def run(state)`. The `/api/trade/stream` endpoint calls `stream()`, the existing `/api/trade` endpoint calls `run()`. Both are in the same file.
- **Option A2 (simpler):** Keep `run()` unchanged, do the streaming entirely in `main.py` using `graph.astream_events()`.

**Recommendation: Option A1.** Keeps the streaming logic co-located with the LLM call, easier to test in isolation.

Rough structure for `strategy_node.stream()`:

```python
async def stream(state: TradeState):
    """
    Async generator variant of run(). Yields SSE-ready dicts:
      {"type": "reasoning_token", "content": "..."}  — one per LLM token
      {"type": "strategy_ready", "strategy": {...}}  — after structured call
    """
    trade = state.get("trade_request", {})
    llm = get_chat_llm(temperature=0.2)

    # Call 1: stream the reasoning
    reasoning_chunks = []
    async for chunk in llm.astream([
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": _build_user_prompt(trade) + "\n\nFirst, explain your reasoning in 2-3 sentences."},
    ]):
        token = chunk.content
        reasoning_chunks.append(token)
        yield {"type": "reasoning_token", "content": token}

    reasoning_text = "".join(reasoning_chunks)

    # Call 2: structured decision using the reasoning as context
    structured_llm = llm.with_structured_output(StrategyOutput)
    result = structured_llm.invoke([
        {"role": "system",    "content": _SYSTEM_PROMPT},
        {"role": "user",      "content": _build_user_prompt(trade)},
        {"role": "assistant", "content": reasoning_text},
        {"role": "user",      "content": "Now output the structured JSON decision."},
    ])

    yield {
        "type": "strategy_ready",
        "strategy": {
            "approach":         result.approach,
            "num_slices":       result.num_slices,
            "shares_per_slice": result.shares_per_slice,
            "reasoning":        reasoning_text,
        }
    }
```

Extract `_build_user_prompt(trade)` as a helper so both `run()` and `stream()` use the same prompt construction.

---

### `backend/main.py` — New SSE endpoint

Add alongside the existing `POST /api/trade`. Keep the polling route unchanged.

```python
from fastapi.responses import StreamingResponse
import json
import asyncio

@app.post("/api/trade/stream")
async def submit_trade_stream(body: TradeRequestBody):
    """
    SSE variant of /api/trade. Streams LLM reasoning tokens in real time,
    then runs C++ simulation and pauses at HITL. Returns text/event-stream.

    Events emitted:
      {"type": "reasoning_token", "content": "..."}
      {"type": "strategy_ready",  "strategy": {...}}
      {"type": "simulation_running"}
      {"type": "paused", "thread_id": "...", "state": {...}}
      {"type": "error",  "detail": "..."}
    """
    thread_id = str(uuid.uuid4())
    config    = {"configurable": {"thread_id": thread_id}}

    async def event_generator():
        try:
            # Build initial state
            state = {
                "trade_request": {
                    "prompt":       body.prompt,
                    "instrument":   body.instrument,
                    "total_shares": body.total_shares,
                    "deadline":     body.deadline,
                },
                "revision_count": 0,
                "errors":         [],
            }

            # Phase 1: stream strategy_node reasoning
            strategy = None
            async for event in strategy_node.stream(state):
                yield f"data: {json.dumps(event)}\n\n"
                if event["type"] == "strategy_ready":
                    strategy = event["strategy"]

            if strategy is None:
                yield f"data: {json.dumps({'type': 'error', 'detail': 'strategy_node failed'})}\n\n"
                return

            # Phase 2: run the full graph from simulation_node onward
            # (graph needs to support entering mid-pipeline — or re-build state and invoke)
            yield f"data: {json.dumps({'type': 'simulation_running'})}\n\n"

            state["strategy"] = strategy
            # Run graph from simulation_node to hitl pause
            result = _graph.invoke(state, config=config)

            # At this point the graph has paused at hitl_node
            yield f"data: {json.dumps({'type': 'paused', 'thread_id': thread_id, 'state': result})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**Note on graph entry point:** If invoking the full graph always starts at `strategy_node`, you'll need to either (a) let the graph run `strategy_node` normally (ignoring streaming) then emit the paused state, OR (b) build a second graph variant that starts at `simulation_node`. Option (a) is simpler — the streaming is done outside the graph, and the graph's own `strategy_node.run()` call is a no-op duplication. Option (b) is cleaner but adds a second graph. Decide in the morning based on how the graph entry routing works.

---

## Phase 2 — Frontend (~1.5 hours)

### `src/app/page.tsx`

Add a new status: `"streaming"` between `"submitting"` and `"awaiting_approval"`.  
Add state for live reasoning text: `const [reasoningText, setReasoningText] = useState("")`.

Replace the submit handler's `fetch + poll` block with an `EventSource` consumer:

```typescript
const handleSubmit = useCallback(async (body: TradeRequestBody) => {
  setStatus("streaming");
  setReasoningText("");
  setTradeState(null);
  setThreadId(null);

  const res = await fetch(`${API}/api/trade/stream`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify(body),
  });

  // Consume the SSE stream via ReadableStream (EventSource doesn't support POST)
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const lines = decoder.decode(value).split("\n");
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const event = JSON.parse(line.slice(6));

      if (event.type === "reasoning_token") {
        setReasoningText(prev => prev + event.content);
      }
      if (event.type === "paused") {
        setThreadId(event.thread_id);
        setTradeState(event.state);
        setStatus("awaiting_approval");
      }
      if (event.type === "error") {
        setErrorMsg(event.detail);
        setStatus("idle");
      }
    }
  }
}, []);
```

**Note:** `EventSource` only supports GET. For POST-initiated SSE, use `fetch` + `ReadableStream` as above. This is the standard pattern for POST SSE in Next.js.

Pass `reasoningText` and `status` down to `HitlPanel` / `StrategyCard`.

---

### `src/app/components/StrategyCard.tsx`

Add a streaming mode. When `isStreaming` is true, render the live text with a cursor. When false (paused state arrived), render the formatted card:

```tsx
interface Props {
  strategy: Strategy | null;
  isStreaming: boolean;
  streamingText: string;
}

export default function StrategyCard({ strategy, isStreaming, streamingText }: Props) {
  if (isStreaming) {
    return (
      <div className="...">
        <div className="text-xs text-gray-500 mb-2">LLM reasoning...</div>
        <p className="text-gray-200 text-sm leading-relaxed">
          {streamingText}
          <span className="animate-pulse">▋</span>
        </p>
      </div>
    );
  }
  // existing formatted card render
}
```

---

## Phase 3 — Wiring + Testing (~1 hour)

1. Run the Azure streaming validation test (top of this doc) first
2. Start backend, open browser console, submit a trade via the new endpoint
3. Watch SSE events in the Network tab → EventStream view in DevTools
4. Verify `reasoning_token` events arrive incrementally (not all at once)
5. Verify `paused` event carries correct `thread_id` and full state for HITL panel
6. Verify existing `POST /api/trade` polling path still works (fallback)
7. Update `README.md` build status: Phase 6 SSE → ✅ Complete
8. Take new DEMO.md screenshots: section 2 (streaming text appearing), section 3 (can be removed or updated)

---

## What Does NOT Change

- `simulation_node.py` — untouched
- `hitl_node.py` — untouched
- `execution_node.py` — untouched
- `graph.py` — may need minor change if graph entry needs to start post-strategy
- `HitlPanel.tsx` — receives `reasoningText` as a prop, passes to `StrategyCard`
- `MetricsCard.tsx` — untouched
- All existing API routes — unchanged, polling path stays live

---

## Risk Log

| Risk | Likelihood | Mitigation |
|---|---|---|
| Azure OpenAI streaming disabled on deployment | Medium | Check first (5-min test at top). Fix in Azure Portal, not in code. |
| `_graph.invoke()` inside async `StreamingResponse` blocks event loop | Medium | Use `asyncio.to_thread(_graph.invoke, ...)` to run sync graph in a thread pool |
| Graph always starts at `strategy_node`, can't enter mid-pipeline | Low | Run strategy_node streaming outside graph, then invoke graph with pre-populated strategy in state |
| `ReadableStream` SSE parsing brittle for multi-line chunks | Low | Buffer incomplete lines across `reader.read()` calls — standard SSE parsing pattern |
