"""Artifact file storage for long-running jobs."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nipux_cli.db import AgentDB, new_id, utc_now

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(value: str, *, default: str = "artifact") -> str:
    cleaned = _SAFE_NAME_RE.sub("-", value.strip()).strip(".-")
    return (cleaned or default)[:96]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredArtifact:
    id: str
    path: Path
    sha256: str
    title: str | None = None
    summary: str | None = None


class ArtifactStore:
    def __init__(self, home: str | Path, db: AgentDB | None = None):
        self.home = Path(home)
        self.db = db
        self.home.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        path = self.home / "jobs" / job_id / "artifacts"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _assert_inside_home(self, path: Path) -> Path:
        resolved = path.resolve()
        root = self.home.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Refusing to read outside agent home: {path}") from exc
        return resolved

    def write_text(
        self,
        *,
        job_id: str,
        content: str,
        title: str | None = None,
        summary: str | None = None,
        artifact_type: str = "text",
        run_id: str | None = None,
        step_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StoredArtifact:
        suffix = "md" if artifact_type in {"digest", "markdown", "text"} else "txt"
        stem = safe_filename(title or artifact_type)
        timestamp = utc_now().replace("+00:00", "Z").replace(":", "")
        filename = f"{timestamp}-{stem}-{new_id('file')}.{suffix}"
        path = self.job_dir(job_id) / filename
        path.write_text(content, encoding="utf-8")
        digest = sha256_text(content)
        artifact_id = new_id("art")
        if self.db is not None:
            artifact_id = self.db.add_artifact(
                job_id=job_id,
                run_id=run_id,
                step_id=step_id,
                path=path,
                sha256=digest,
                artifact_type=artifact_type,
                title=title,
                summary=summary,
                metadata=metadata,
            )
        return StoredArtifact(id=artifact_id, path=path, sha256=digest, title=title, summary=summary)

    def read_text(self, artifact_id_or_path: str) -> str:
        path = Path(artifact_id_or_path)
        if self.db is not None and not path.exists():
            path = Path(self.db.get_artifact(artifact_id_or_path)["path"])
        safe_path = self._assert_inside_home(path)
        return safe_path.read_text(encoding="utf-8")

    def search_text(self, *, job_id: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        query_lower = query.lower().strip()
        results: list[dict[str, Any]] = []
        for artifact in self.db.list_artifacts(job_id, limit=250):
            haystack = " ".join(
                str(artifact.get(key) or "") for key in ("title", "summary", "type")
            ).lower()
            content = ""
            if query_lower and query_lower not in haystack:
                try:
                    content = self.read_text(artifact["id"])
                except OSError:
                    content = ""
                if query_lower not in content.lower():
                    continue
            elif not query_lower:
                try:
                    content = self.read_text(artifact["id"])
                except OSError:
                    content = ""
            if not content:
                try:
                    content = self.read_text(artifact["id"])
                except OSError:
                    content = ""
            excerpt = content[:500]
            if query_lower:
                idx = content.lower().find(query_lower)
                if idx >= 0:
                    start = max(0, idx - 160)
                    excerpt = content[start:start + 500]
            results.append({
                "id": artifact["id"],
                "title": artifact.get("title"),
                "type": artifact.get("type"),
                "path": artifact.get("path"),
                "summary": artifact.get("summary"),
                "excerpt": excerpt,
            })
            if len(results) >= limit:
                break
        return results
