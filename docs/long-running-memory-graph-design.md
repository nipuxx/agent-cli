# Long-Running Memory Graph Design

Nipux needs long-running workers that keep improving instead of flattening into repeated search, notes, or shallow checkpoints. The backend now treats each job as having a small durable "brain": a job-local memory graph made of connected nodes and links. It is not task-specific and does not require embeddings or a new service to be useful.

## Research Takeaways

- **Complementary learning systems:** human memory separates fast episodic capture from slower semantic consolidation. The hippocampus rapidly stores separated episodes while cortex gradually extracts structure. Nipux mirrors this with recent events/steps as fast episodic traces and `memory_graph` nodes as consolidated reusable knowledge. Source: [O'Reilly and Norman, 2002](https://collaborate.princeton.edu/en/publications/hippocampal-and-neocortical-contributions-to-memory-advances-in-t) and [McClelland et al., 1995](https://colab.ws/articles/10.1037/0033-295x.102.3.419).
- **Sleep/consolidation:** memory consolidation strengthens relevant traces and reorganizes them into associations that support later inference. Nipux should periodically turn raw work into compact graph nodes and edges instead of replaying full history. Source: [Born and Wilhelm, 2012](https://link.springer.com/article/10.1007/s00426-011-0335-6) and [Diekelmann and Born, 2010](https://www.nature.com/articles/nrn2762).
- **Reflexion:** agents improve without weight updates by writing verbal reflections into episodic memory after feedback. Nipux already has lessons and reflection; the graph adds structure so reflections can connect to facts, decisions, tasks, and evidence. Source: [Reflexion](https://huggingface.co/papers/2303.11366).
- **Generative Agents:** believable long-lived agents combine memory stream, retrieval, reflection, and planning. Nipux should keep the event stream, but retrieve distilled context through durable ledgers and graph nodes. Source: [Generative Agents](https://huggingface.co/papers/2304.03442).
- **MemGPT:** OS-style memory tiers let fixed-context models use long histories by paging between prompt context and archival memory. Nipux's prompt now gets only a ranked slice of graph memory, with `search_memory_graph` for deeper recall. Source: [MemGPT](https://huggingface.co/papers/2310.08560).
- **Voyager:** long-horizon improvement comes from an automatic curriculum, a growing reusable skill library, and iterative self-verification. Nipux's graph supports this by representing skills, strategies, open questions, decisions, and evidence links as reusable nodes. Source: [Voyager](https://voyager.minedojo.org/).
- **Agent memory surveys and graph memory work:** recent surveys and systems emphasize memory operations: write, retrieve, update, consolidate, forget/deprecate, and evaluate. Graph memory helps preserve relationships and temporal change better than a flat note list. Sources: [LLM Agent Memory Survey](https://huggingface.co/papers/2404.13501), [AriGraph](https://huggingface.co/papers/2407.04363), [Zep](https://huggingface.co/papers/2501.13956).

## Backend Shape

Each job can now maintain metadata under `memory_graph`:

- `nodes`: connected notes with `kind`, `status`, `summary`, `salience`, `confidence`, `tags`, `parent_key`, `links`, and `evidence_refs`.
- `edges`: typed links between nodes such as `supports`, `replaces`, `raises`, `blocks`, or `depends_on`.
- Nodes are generic: `episode`, `fact`, `strategy`, `skill`, `question`, `decision`, `constraint`, `artifact`, `source`, `task`, `experiment`, and `milestone`.

The worker gets a compact `Memory graph` prompt section that ranks active, salient, recent, and procedural nodes. It can call `search_memory_graph` when it needs deeper recall. It can call `record_memory_graph` whenever new work should become reusable knowledge.

Operators can inspect the same graph with `nipux memory --graph`, which writes a self-contained clickable HTML artifact. The view uses a local canvas renderer, needs no external network assets, and lets the operator rotate, zoom, search, and click nodes to inspect summaries, evidence refs, tags, and links.

The worker also has a generic consolidation guard: once findings, sources, experiments, lessons, resolved tasks, or roadmap milestones accumulate faster than graph nodes and links, more branch churn is blocked until the worker calls `record_memory_graph` or records why the current branch has no reusable memory value.

## Live Model Smoke

Use `scripts/live_memory_graph_smoke.py` to verify a real OpenAI-compatible model can follow the graph-consolidation contract. The script creates a temporary Nipux home, disables side-effect tools, seeds generic durable job state, and runs a few worker turns. It succeeds only after the model calls `record_memory_graph` and creates at least one node.

Example:

```bash
OPENROUTER_API_KEY=... uv run python scripts/live_memory_graph_smoke.py --model qwen/qwen3.6-27b
```

The key is read from the configured environment variable and is never printed. If no key is present, the script exits before making a network request.

Latest smoke result:

- Model: `qwen/qwen3.6-27b`
- Provider path: OpenAI-compatible chat completions through OpenRouter
- Isolation: temporary Nipux home with browser, web, shell, and file tools disabled
- Result: first worker step called `record_memory_graph`
- Graph written: 7 nodes and 8 edges

## Why This Should Improve Long Runs

- Raw history stays available in events/artifacts, but the model sees a compact graph slice.
- Bad or small models get explicit, typed memory instead of relying on implicit recap.
- Repeated branches can be deprecated instead of merely summarized.
- Useful strategies and skills can compound across hundreds or thousands of actions.
- Open questions remain visible as first-class nodes, making it harder for the worker to drift away from unresolved blockers.

## Next Backend Slices

- Add periodic deterministic consolidation that proposes graph nodes from recent events when the model fails to do it.
- Tune graph-aware stagnation checks from real runs: if a branch has no new node, edge, validation, experiment, or deliverable after a budget, force consolidation or branch rejection.
- Add better retrieval scoring using local embeddings when available, while keeping lexical fallback mandatory.
- Add live UI/status counters for memory graph growth: new nodes, active questions, deprecated paths, and current strategy.
