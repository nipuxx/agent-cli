import pytest

from nipux_cli.artifacts import ArtifactStore
from nipux_cli.db import AgentDB


def test_artifact_store_writes_reads_and_searches(tmp_path):
    db = AgentDB(tmp_path / "state.db")
    try:
        job_id = db.create_job("Collect findings")
        store = ArtifactStore(tmp_path, db=db)

        stored = store.write_text(
            job_id=job_id,
            title="Findings list",
            summary="contains acme finding",
            content="Acme Corp\ncontact: founder@example.com\n",
        )

        assert store.read_text(stored.id).startswith("Acme Corp")
        results = store.search_text(job_id=job_id, query="founder", limit=5)
        assert results[0]["id"] == stored.id
        assert "founder@example.com" in results[0]["excerpt"]
    finally:
        db.close()


def test_artifact_store_rejects_paths_outside_home(tmp_path):
    store = ArtifactStore(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope", encoding="utf-8")

    with pytest.raises(ValueError):
        store.read_text(str(outside))
