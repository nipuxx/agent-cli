from nipux_cli.templates import program_for_job


def test_generic_template_pushes_artifacts_and_updates():
    program = program_for_job(kind="generic", title="research", objective="Find findings")

    assert "Save important observations as artifacts" in program
    assert "Use report_update" in program
    assert "Use record_lesson" in program
    assert "record_source" in program
    assert "record_findings" in program
    assert "record_tasks" in program
    assert "record_mission" in program
    assert "record_mission_validation" in program
    assert "record_findings" in program
