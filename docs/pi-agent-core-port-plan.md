# Pi Agent Core Port Plan

Research date: 2026-04-30

Sources:
- https://github.com/badlogic/pi-mono
- https://github.com/badlogic/pi-mono/tree/main/packages/agent
- https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent
- https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/session.md
- https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/sdk.md
- https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/rpc.md

Pi is MIT licensed, so direct adaptation is allowed if we preserve attribution
where substantial code is ported. The right move is not to copy the full
TypeScript app into Nipux. The right move is to port the small generic runtime
ideas from `packages/agent` and keep Nipux's SQLite daemon, tools, and
multi-job persistence.

## What Pi Does Better

Pi's core is a stateful agent loop, not a "one next action" prompt wrapper.
The important files are:

- `packages/agent/src/agent-loop.ts`
- `packages/agent/src/agent.ts`
- `packages/agent/src/types.ts`
- `packages/coding-agent/src/core/session-manager.ts`
- `packages/coding-agent/src/core/agent-session.ts`
- `packages/coding-agent/src/core/compaction/*`

Key behaviors to port:

1. Evented loop as the runtime contract.
   Pi emits `agent_start`, `turn_start`, `message_start`, `message_update`,
   `message_end`, `tool_execution_start`, `tool_execution_update`,
   `tool_execution_end`, `turn_end`, and `agent_end`. Nipux has events, but
   currently treats a daemon step as the main unit. We should make these events
   first-class and derive UI/status from them.

2. Real transcript state.
   Pi keeps `AgentMessage[]` as state and converts it to LLM messages only at
   the model boundary. Nipux currently rebuilds each prompt from job metadata,
   memory, recent steps, and ledgers. That works, but it loses the clean
   distinction between visible transcript, UI-only records, and model context.

3. Context transform boundary.
   Pi uses `transformContext(messages)` before `convertToLlm(messages)`. This is
   the exact place Nipux should inject durable operator context, compact memory,
   task contracts, ledgers, and active constraints without polluting raw history.

4. Steering and follow-up queues.
   Pi splits queued user input into:
   - `steer`: delivered after current tool execution and before the next model turn.
   - `followUp`: delivered only when the agent would otherwise stop.
   Nipux already has `steer` and `follow_up` metadata, but delivery is bolted
   onto single steps. This should move into the agent core.

5. Hookable tool preflight and postprocessing.
   Pi has `beforeToolCall` and `afterToolCall`. Nipux has good generic guards,
   but they live inside `worker.py`. They should become hooks:
   - duplicate/repetition guard
   - artifact obligation guard
   - measurement obligation guard
   - source quality guard
   - experiment accounting after measurable shell output

6. Tool batch semantics.
   Pi can prepare tool calls sequentially, execute safe tools in parallel, and
   still persist tool-result messages in assistant order. Nipux currently
   executes only the first tool call. That leaves useful model intent unused.

7. Compaction as session structure, not just memory refresh.
   Pi stores compaction entries in the session tree. Full history remains, but
   future model context sees a summary plus kept recent messages. Nipux has
   `memory_index`, but it should add explicit compaction entries tied to the
   transcript path.

8. Continue semantics.
   Pi has `continue()` for retries after errors or compaction. Nipux currently
   creates a new run every step. Continue semantics would make recovery cleaner
   after model errors, context overflow, daemon restart, and queued messages.

## Nipux Mapping

Current Nipux files:

- `nipux_cli/worker.py`: prompt building, step execution, guards, reflection.
- `nipux_cli/daemon.py`: forever loop, lock, heartbeat, multi-job scheduling.
- `nipux_cli/db.py`: SQLite state, events, job metadata, ledgers.
- `nipux_cli/operator_context.py`: durable operator message filtering.
- `nipux_cli/tools.py`: tool registry and tool execution.
- `nipux_cli/compression.py`: compact memory refresh.
- `nipux_cli/cli.py`: chat/TUI/status/output rendering.

Target files:

- Add `nipux_cli/agent_core.py`
  - Python port of Pi's small `Agent`, `PendingMessageQueue`, event types,
    tool result types, and loop control.
  - Keep attribution header because it is directly inspired by Pi's MIT code.
  - Support non-streaming model responses first, then streaming later.

- Add `nipux_cli/session.py`
  - Load/save transcript entries for a job.
  - Build current session context from entries plus compaction records.
  - Keep SQLite as source of truth instead of JSONL files, but use Pi's entry
    shape: `message`, `compaction`, `branch_summary`, `custom`,
    `custom_message`, `model_change`, and `label`.

- Refactor `nipux_cli/worker.py`
  - Move prompt assembly into `transform_context`.
  - Move `_blocked_tool_call_result` into `before_tool_call`.
  - Move measurement/source/artifact side effects into `after_tool_call`.
  - Replace "only first tool call" execution with core loop tool execution.
  - Preserve one bounded heartbeat by limiting wall-clock/tool budget per daemon
    tick, not by discarding the agent loop structure.

- Extend `nipux_cli/db.py`
  - Add a `session_entries` table:
    - `id`
    - `job_id`
    - `parent_id`
    - `entry_type`
    - `created_at`
    - `payload_json`
  - Add `job_session_state` metadata for current leaf and compaction stats.
  - Backfill existing `events`/`steps` into session view lazily.

- Keep `nipux_cli/daemon.py`
  - Do not replace the daemon. Pi is mostly single-session interactive; Nipux
    needs multi-job background scheduling.
  - The daemon should call `AgentSession.continue_or_prompt()` for whichever job
    is runnable, then keep heartbeating while the agent loop emits events.

## Implementation Sequence

### Commit 1: Agent Core Skeleton

Create `agent_core.py` with:

- `AgentMessage`
- `AgentToolCall`
- `AgentToolResult`
- `AgentEvent`
- `PendingMessageQueue`
- `AgentState`
- `Agent`

Support:

- `prompt(messages)`
- `continue_()`
- `steer(message)`
- `follow_up(message)`
- `abort()`
- `wait_for_idle()`
- event subscription
- sequential tool execution only
- `before_tool_call`
- `after_tool_call`
- `transform_context`
- `convert_to_llm`

Tests:

- event order matches Pi's documented event order
- steering is delivered after tool execution
- follow-up waits until no tool calls remain
- prompt/continue reject concurrent runs
- tool errors become tool result messages instead of crashing the loop

### Commit 2: Session Entries

Add SQLite session entries and a `SessionManager` equivalent.

Tests:

- append messages with parent IDs
- build context from current leaf
- compaction summary appears before kept messages
- full raw history remains queryable
- branch summaries can be represented even if UI does not expose branching yet

### Commit 3: Worker Integration

Make `run_one_step` use the Pi-style agent loop.

Important constraint:

Nipux should still be generic and background-safe. Do not encode any objective,
host, model, source, or task domain. The old guards stay generic and move into
hooks.

Tests:

- model can call multiple tools and all are persisted in order
- duplicate/measurement/artifact guards block through `before_tool_call`
- measurable output creates obligations through `after_tool_call`
- operator steer persists until acknowledged and is injected through the queue
- follow-up waits behind active branch work

### Commit 4: Compaction

Replace fixed memory refresh as the main context strategy with session
compaction:

- estimate context from last usage when available
- truncate tool results for summarization
- store compaction entries
- rebuild model context from compaction plus recent path
- continue after overflow or threshold compaction

Tests:

- long transcript compacts without losing recent messages
- compaction failure does not crash daemon
- queued messages survive compaction
- context overflow retry uses `continue_()`

### Commit 5: UI/Event Stream Cleanup

Make CLI/TUI read the event stream and session entries, not ad hoc step text.

Tests:

- chat shows user/assistant transcript
- right pane shows job/daemon/session stats
- activity shows tool start/update/end events
- history can show full transcript, compacted transcript, and raw events

## Why This Should Fix The Current Failure Mode

Nipux currently has many good guards, but the model is still treated like a
stateless planner that gets one tool call per daemon step. That encourages
research churn because the loop boundary is outside the model's natural
tool-result feedback cycle.

Pi's design keeps the model inside a coherent turn loop:

1. User/operator/context enters as messages.
2. Assistant proposes tool calls.
3. Tools execute and return tool-result messages.
4. The assistant immediately sees those results.
5. Steering and follow-up are delivered at well-defined boundaries.
6. Compaction preserves the useful path instead of stuffing every summary into
   every future prompt.

Porting that structure should make Nipux feel less like a step counter and more
like an actual long-running agent runtime.

## What Not To Copy

Do not copy Pi's task-specific extension examples into Nipux core.

Do not make the harness depend on Node, Bun, or the Pi TUI.

Do not encode any SSH, model, inference, lead-finding, browser-source, or local
machine assumptions. Everything here must stay generic:

- transcript
- events
- queues
- tool hooks
- compaction
- session state
- UI rendering over events

