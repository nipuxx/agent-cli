from nipux_cli.planning import initial_roadmap_for_objective, initial_task_contract


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
