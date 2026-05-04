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
    "show tokens": "usage",
    "show token usage": "usage",
    "context usage": "usage",
    "token usage": "usage",
    "how much did it cost": "usage",
    "how many tokens did it use": "usage",
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
    "show worker activity": "activity",
    "show worker output": "activity",
    "show raw work": "outputs",
    "show console output": "outputs",
    "show logs": "outputs",
    "show saved files": "artifacts",
    "what did it save": "artifacts",
    "what files did it create": "artifacts",
    "what outputs did it save": "artifacts",
    "what tasks are open": "tasks",
    "what is the current task": "tasks",
    "show measurements": "experiments",
    "show benchmarks": "experiments",
    "show milestones": "roadmap",
    "show plan": "roadmap",
    "show daemon": "health",
    "start daemon": "start",
    "restart daemon": "restart",
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
    control_phrase = _looks_like_control_phrase(lowered)
    if control_phrase and _mentions_any(lowered, ("token", "cost", "usage", "context window", "context budget")):
        return "/usage"
    if control_phrase and _mentions_any(lowered, ("tool call", "tool calls", "worker activity", "worker output", "right pane")):
        return "/activity"
    if control_phrase and _mentions_any(lowered, ("console output", "raw output", "raw run", "raw runs", "log", "logs")):
        return "/outputs"
    if control_phrase and _mentions_any(lowered, ("saved file", "saved files", "artifact", "artifacts")):
        return "/artifacts"
    if (
        _mentions_any(lowered, ("what did", "what has", "what have", "show me"))
        and _mentions_any(lowered, ("made", "created", "saved", "produced", "done", "accomplished"))
    ):
        return "/outcomes"
    if control_phrase and _mentions_any(lowered, ("measurement", "measurements", "experiment", "experiments", "benchmark", "benchmarks")):
        return "/experiments"
    if control_phrase and _mentions_any(lowered, ("roadmap", "milestone", "milestones", "plan")):
        return "/roadmap"
    if control_phrase and _mentions_any(lowered, ("task", "tasks", "todo", "to do", "queue")):
        return "/tasks"
    if control_phrase and _mentions_any(lowered, ("finding", "findings")):
        return "/findings"
    if control_phrase and _mentions_any(lowered, ("source", "sources")):
        return "/sources"
    if control_phrase and _mentions_any(lowered, ("lesson", "lessons", "learned")):
        return "/lessons"
    if control_phrase and _mentions_any(lowered, ("memory", "remembered", "learning state")):
        return "/memory"
    if lowered in {"start daemon", "launch daemon"}:
        return "/start"
    if lowered in {"restart daemon", "reload daemon"}:
        return "/restart"
    if lowered in {"jobs", "show jobs", "list jobs", "switch jobs", "change jobs"}:
        return "/jobs"
    if lowered in {"settings", "show settings"}:
        return "/model"
    if lowered in {"model settings", "change model", "edit settings"}:
        return "/model"
    if lowered in {
        "run",
        "start",
        "run job",
        "start job",
        "start working",
        "start work",
        "run this",
        "run the job",
        "run this job",
        "start the job",
        "start this job",
        "continue",
        "keep going",
        "keep working",
        "resume work",
    }:
        return "/run"
    if lowered in {
        "pause",
        "pause job",
        "pause the job",
        "pause work",
        "pause this job",
        "stop",
        "stop job",
        "stop the job",
        "stop work",
        "stop working",
        "stop this job",
        "halt",
        "halt job",
        "halt the job",
    }:
        return "/pause"
    if lowered in {"resume", "resume job", "resume the job", "resume this job", "reopen this job"}:
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


def _mentions_any(text: str, needles: tuple[str, ...]) -> bool:
    for needle in needles:
        if " " in needle:
            if needle in text:
                return True
            continue
        if re.search(rf"\b{re.escape(needle)}\b", text):
            return True
    return False


def _looks_like_control_phrase(text: str) -> bool:
    return text.startswith(
        (
            "show ",
            "view ",
            "open ",
            "list ",
            "display ",
            "give me ",
            "where ",
            "what ",
            "how ",
            "is ",
            "are ",
            "check ",
        )
    )


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
