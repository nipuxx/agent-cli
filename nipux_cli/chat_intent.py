"""Natural-language intent parsing for Nipux chat and shell control."""

from __future__ import annotations

import re


NATURAL_COMMANDS = {
    "tell me updates": "updates",
    "show updates": "updates",
    "show outcomes": "outcomes",
    "show all outcomes": "outcomes all",
    "show all accomplishments": "outcomes all",
    "show accomplishments": "outcomes",
    "what have all jobs done": "outcomes all",
    "what has everything done": "outcomes all",
    "what did all jobs do": "outcomes all",
    "what did it accomplish": "outcomes",
    "what has it done": "outcomes",
    "what has it done so far": "outcomes",
    "what have you done": "outcomes",
    "what have you done so far": "outcomes",
    "what did it actually do": "outcomes",
    "what did the model do": "outcomes",
    "show me what it did": "outcomes",
    "show history": "history",
    "what happened": "history",
    "show events": "events",
    "what did it find": "updates",
    "what did you find": "updates",
    "what has it found": "updates",
    "findings": "findings",
    "tasks": "tasks",
    "roadmap": "roadmap",
    "show roadmap": "roadmap",
    "show artifacts": "artifacts",
    "where are artifacts": "artifacts",
    "show lessons": "lessons",
    "what did it learn": "lessons",
    "show findings": "findings",
    "show tasks": "tasks",
    "show experiments": "experiments",
    "show sources": "sources",
    "show memory": "memory",
    "show metrics": "metrics",
    "show usage": "usage",
    "show cost": "usage",
    "token usage": "usage",
    "how much did it cost": "usage",
    "what is going on": "status",
    "whats going on": "status",
    "what's going on": "status",
    "what are you doing": "status",
    "what is it doing": "status",
    "how is it going": "status",
    "how are things going": "status",
    "check up on things": "status",
    "what is blocking it": "status",
    "what's blocking it": "status",
    "why is it stuck": "status",
    "is it stuck": "status",
    "is it running": "health",
    "is the daemon running": "health",
    "daemon health": "health",
    "show health": "health",
    "show activity": "activity",
    "show tool calls": "activity",
}


def natural_command_for(text: str) -> str:
    return NATURAL_COMMANDS.get(" ".join(text.strip().lower().split()), "")


def chat_control_command(line: str) -> str:
    text = " ".join(line.strip().split())
    if not text:
        return ""
    lowered = text.lower().rstrip("?.!")
    natural = NATURAL_COMMANDS.get(lowered)
    if natural:
        return f"/{natural}"
    if lowered in {"jobs", "show jobs", "list jobs", "switch jobs", "change jobs"}:
        return "/jobs"
    if lowered in {"settings", "show settings"}:
        return "/model"
    if lowered in {"model settings", "change model", "edit settings"}:
        return "/model"
    if lowered in {
        "run",
        "start",
        "start working",
        "start work",
        "run this",
        "run this job",
        "start this job",
        "continue",
        "keep going",
        "keep working",
        "resume work",
    }:
        return "/run"
    if lowered in {"pause", "pause work", "pause this job", "stop", "stop work", "stop working", "stop this job"}:
        return "/pause"
    if lowered in {"resume", "resume this job", "reopen this job"}:
        return "/resume"
    if lowered in {"history", "show history", "timeline", "show timeline"}:
        return "/history"
    if lowered in {
        "all outcomes",
        "show all outcomes",
        "show all accomplishments",
        "what have all jobs done",
        "what has everything done",
        "what did all jobs do",
    }:
        return "/outcomes all"
    if lowered in {
        "outcomes",
        "show outcomes",
        "accomplishments",
        "show accomplishments",
        "what has it done",
        "what has it done so far",
        "what have you done",
        "what have you done so far",
        "what did it actually do",
        "what did the model do",
        "show me what it did",
    }:
        return "/outcomes"
    if lowered in {"artifacts", "outputs", "saved outputs", "show artifacts", "show outputs"}:
        return "/artifacts"
    if lowered in {"memory", "show memory", "learning", "show learning"}:
        return "/memory"
    return ""


def message_requests_immediate_run(message: str) -> bool:
    lowered = " ".join(message.strip().lower().split())
    if message_requests_queued_job(message):
        return False
    if re.match(r"^(?:please\s+)?(?:start|launch|run|spin\s+off)\b", lowered):
        return True
    return bool(re.search(r"\b(?:and|then)\s+(?:start|launch|run|resume)\s+(?:it|the\s+job|work)?\b", lowered))


def message_requests_queued_job(message: str) -> bool:
    lowered = " ".join(message.strip().lower().split())
    return bool(
        re.search(
            r"\b(?:queue only|plan only|create only|do not start|don't start|do not run|don't run|without starting)\b",
            lowered,
        )
    )


def extract_job_objective_from_message(message: str) -> str:
    text = " ".join(message.strip().split())
    if not text:
        return ""
    lowered = text.lower()
    patterns = [
        r"^(?:please\s+)?(?:create|start|spin\s+off|make|launch)\s+(?:a\s+)?(?:new\s+)?job\s+(?:to|for|that|which)?\s*(.+)$",
        r"^(?:please\s+)?(?:send|queue)\s+(?:off\s+)?(?:a\s+)?(?:new\s+)?job\s+(?:to|for|that|which)?\s*(.+)$",
        r"^(?:please\s+)?(?:new|job)\s+(.+)$",
        r"^(?:please\s+)?(?:can\s+you|could\s+you|i\s+need\s+you\s+to|i\s+want\s+you\s+to)\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            objective = match.group(1).strip(" .")
            return objective if looks_like_job_objective(objective) else ""
    if looks_like_job_objective(text) and not looks_like_smalltalk(lowered):
        return text
    return ""


def looks_like_smalltalk(lowered: str) -> bool:
    return lowered in {"hi", "hello", "hey", "yo", "sup", "thanks", "thank you"} or lowered.endswith("?")


def looks_like_job_objective(text: str) -> bool:
    lowered = text.lower()
    if len(text.split()) < 3:
        return False
    action_words = {
        "research",
        "monitor",
        "optimize",
        "build",
        "find",
        "test",
        "deploy",
        "fix",
        "write",
        "analyze",
        "audit",
        "track",
        "benchmark",
        "create",
        "document",
        "draft",
        "generate",
        "scrape",
        "produce",
        "watch",
        "automate",
        "summarize",
        "compare",
        "investigate",
        "improve",
    }
    return any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in action_words)
