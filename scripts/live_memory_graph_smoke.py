#!/usr/bin/env python3
"""Run an opt-in real-model smoke test for memory-graph tool calling.

This script is intentionally outside the normal Nipux runtime path. It creates
an isolated temporary Nipux home, seeds generic durable job state, and verifies
that a configured OpenAI-compatible model can consolidate that state with the
`record_memory_graph` tool.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from nipux_cli.config import AppConfig, ModelConfig, RuntimeConfig, ToolAccessConfig
from nipux_cli.db import AgentDB
from nipux_cli.memory_graph import memory_graph_from_job
from nipux_cli.worker import run_one_step


DEFAULT_MODEL = "qwen/qwen3.6-27b"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenAI-compatible model name. Default: {DEFAULT_MODEL}")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Provider base URL. Default: {DEFAULT_BASE_URL}")
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV, help=f"API key env var. Default: {DEFAULT_API_KEY_ENV}")
    parser.add_argument("--context-length", type=int, default=262_144)
    parser.add_argument("--steps", type=int, default=3, help="Maximum worker turns to try.")
    parser.add_argument("--keep-home", action="store_true", help="Keep the temporary Nipux home for inspection.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable result.")
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        return _finish(
            {
                "success": False,
                "error": f"{args.api_key_env} is not set",
                "action": f"Export {args.api_key_env} before running this live smoke. The key is never printed.",
            },
            json_output=args.json,
        )

    home = Path(tempfile.mkdtemp(prefix="nipux-memory-graph-live-"))
    try:
        config = AppConfig(
            runtime=RuntimeConfig(home=home, max_steps_per_run=1),
            model=ModelConfig(
                model=args.model,
                base_url=args.base_url.rstrip("/"),
                api_key_env=args.api_key_env,
                context_length=args.context_length,
                request_timeout_seconds=180,
            ),
            tools=ToolAccessConfig(browser=False, web=False, shell=False, files=False),
        )
        config.ensure_dirs()
        db = AgentDB(config.runtime.state_db_path)
        try:
            job_id = db.create_job(
                "Consolidate generic durable job knowledge into an inspectable memory graph.",
                title="memory graph live smoke",
                metadata=_seed_metadata(),
            )
            db.update_job_status(job_id, "running")
            executions = []
            for _ in range(max(1, args.steps)):
                execution = run_one_step(job_id, config=config, db=db)
                executions.append(_execution_summary(execution))
                job = db.get_job(job_id)
                graph = memory_graph_from_job(job)
                if graph["nodes"]:
                    return _finish(
                        {
                            "success": True,
                            "home": str(home),
                            "model": args.model,
                            "base_url": args.base_url,
                            "job_id": job_id,
                            "node_count": len(graph["nodes"]),
                            "edge_count": len(graph["edges"]),
                            "executions": executions,
                        },
                        json_output=args.json,
                    )
            job = db.get_job(job_id)
            graph = memory_graph_from_job(job)
            return _finish(
                {
                    "success": False,
                    "home": str(home),
                    "model": args.model,
                    "base_url": args.base_url,
                    "job_id": job_id,
                    "node_count": len(graph["nodes"]),
                    "edge_count": len(graph["edges"]),
                    "executions": executions,
                    "error": "model did not create memory graph nodes within the step budget",
                },
                json_output=args.json,
            )
        finally:
            db.close()
    finally:
        if args.keep_home:
            print(f"kept temporary Nipux home: {home}", file=sys.stderr)
        else:
            shutil.rmtree(home, ignore_errors=True)


def _seed_metadata() -> dict[str, Any]:
    return {
        "finding_ledger": [
            {
                "name": "Durable outputs need reusable summaries",
                "category": "process",
                "reason": "Saved outputs are easier to reuse when connected to decisions and tasks.",
                "score": 0.82,
            },
            {
                "name": "Repeated branch work needs explicit rejection criteria",
                "category": "process",
                "reason": "A branch should either improve evidence, produce a deliverable, or be deprecated.",
                "score": 0.78,
            },
        ],
        "source_ledger": [
            {
                "source": "internal://recent-events",
                "source_type": "job_history",
                "usefulness_score": 0.8,
                "yield_count": 2,
                "last_outcome": "Recent events exposed reusable process knowledge.",
            },
            {
                "source": "internal://saved-outputs",
                "source_type": "artifact_index",
                "usefulness_score": 0.7,
                "yield_count": 1,
                "last_outcome": "Saved outputs provide evidence refs for future graph nodes.",
            },
        ],
        "lessons": [
            {
                "category": "strategy",
                "lesson": "Prefer measured or validated progress over activity counts.",
                "confidence": 0.86,
            },
            {
                "category": "memory",
                "lesson": "Consolidate stable findings into linked graph nodes before context grows.",
                "confidence": 0.9,
            },
        ],
        "task_queue": [
            {
                "title": "Create a connected memory graph from durable signals",
                "status": "open",
                "output_contract": "decision",
                "acceptance_criteria": "At least one reusable node connected to evidence or strategy.",
            }
        ],
        "roadmap": {
            "title": "Long-running job memory",
            "status": "active",
            "milestones": [
                {
                    "title": "Consolidate reusable knowledge",
                    "status": "open",
                    "validation_contract": "Future turns can retrieve the key decisions without replaying raw history.",
                }
            ],
        },
    }


def _execution_summary(execution: Any) -> dict[str, Any]:
    result = execution.result if isinstance(execution.result, dict) else {}
    return {
        "status": execution.status,
        "tool": execution.tool_name,
        "step_id": execution.step_id,
        "success": result.get("success"),
        "error": result.get("error"),
        "added_nodes": result.get("added_nodes"),
        "added_edges": result.get("added_edges"),
    }


def _finish(payload: dict[str, Any], *, json_output: bool) -> int:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_human_summary(payload))
    return 0 if payload.get("success") else 1


def _human_summary(payload: dict[str, Any]) -> str:
    lines = [f"success: {bool(payload.get('success'))}"]
    for key in ("model", "base_url", "home", "job_id", "node_count", "edge_count", "error", "action"):
        if payload.get(key) is not None:
            lines.append(f"{key}: {payload[key]}")
    executions = payload.get("executions")
    if isinstance(executions, list) and executions:
        lines.append("executions:")
        for item in executions:
            lines.append(
                "  - "
                f"status={item.get('status')} tool={item.get('tool')} "
                f"success={item.get('success')} error={item.get('error')}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
