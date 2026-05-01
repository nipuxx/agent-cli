from nipux_cli.planning import initial_plan_for_objective, initial_roadmap_for_objective, initial_task_contract, objective_profiles


def test_initial_task_contracts_are_generic_and_complete():
    for title in [
        "Clarify the exact success criteria and constraints.",
        "Map the first research or execution branches.",
        "Collect evidence and save outputs as files.",
        "Reflect on what worked, update memory, and continue with the next branch.",
    ]:
        contract = initial_task_contract(title)

        assert contract["output_contract"] in {"research", "artifact", "experiment", "action", "monitor", "decision", "report"}
        assert contract["acceptance_criteria"]
        assert contract["evidence_needed"]
        assert contract["stall_behavior"]


def test_initial_roadmap_uses_valid_generic_contracts():
    roadmap = initial_roadmap_for_objective(title="paper", objective="write a paper")

    for milestone in roadmap["milestones"]:
        for feature in milestone["features"]:
            assert feature["output_contract"] in {
                "research",
                "artifact",
                "experiment",
                "action",
                "monitor",
                "decision",
                "report",
            }


def test_initial_plan_adapts_to_measurable_objectives():
    plan = initial_plan_for_objective("optimize a generic process for lower latency and higher throughput")
    contracts = [initial_task_contract(title)["output_contract"] for title in plan["tasks"]]

    assert plan["profile"] == "measured"
    assert "experiment" in contracts
    assert any("baseline" in title.lower() for title in plan["tasks"])
    assert any("metric" in question.lower() for question in plan["questions"])


def test_initial_plan_adapts_to_deliverable_objectives():
    plan = initial_plan_for_objective("write a full research paper from evidence")
    contracts = [initial_task_contract(title)["output_contract"] for title in plan["tasks"]]

    assert plan["profile"] == "deliverable"
    assert "report" in contracts
    assert any("draft" in title.lower() or "report" in title.lower() for title in plan["tasks"])
    assert any("revise" in title.lower() and "evidence" in title.lower() for title in plan["tasks"])


def test_initial_plan_treats_generated_files_as_deliverables():
    plan = initial_plan_for_objective("generate a polished launch checklist for this repository")
    contracts = [initial_task_contract(title)["output_contract"] for title in plan["tasks"]]

    assert plan["profile"] == "deliverable"
    assert "report" in contracts
    assert any("audience" in question.lower() for question in plan["questions"])


def test_initial_plan_adapts_to_monitoring_objectives():
    plan = initial_plan_for_objective("monitor a recurring process and report important changes")
    contracts = [initial_task_contract(title)["output_contract"] for title in plan["tasks"]]

    assert plan["profile"] == "monitor"
    assert "monitor" in contracts
    assert any("cadence" in question.lower() or "check" in question.lower() for question in plan["questions"])


def test_objective_profiles_stay_generic():
    profiles = objective_profiles("investigate build quality and compare output changes")

    assert profiles
    assert all(profile in {"measured", "deliverable", "monitor", "implementation", "research", "general"} for profile in profiles)
