from nipux_cli.measurement import measurement_candidates, measurement_candidates_are_diagnostic_only


def test_measurement_candidates_extract_markdown_table_unit_columns():
    output = {
        "stdout": (
            "| model                          |       size | backend    | threads |            test |                  t/s |\n"
            "| ------------------------------ | ---------: | ---------- | ------: | --------------: | -------------------: |\n"
            "| example model                  |  11.71 GiB | CPU        |      24 |            pp32 |          5.48 ± 0.11 |\n"
            "| example model                  |  11.71 GiB | CPU        |      24 |           tg128 |          3.44 ± 0.05 |\n"
        )
    }

    candidates = measurement_candidates(output, command="run benchmark")

    assert "pp32 5.48 ± 0.11 t/s" in candidates
    assert "tg128 3.44 ± 0.05 t/s" in candidates
    assert not measurement_candidates_are_diagnostic_only(candidates, command="run benchmark")


def test_measurement_candidates_extract_generic_table_metrics():
    output = {
        "stdout": (
            "| benchmark | latency | req/s |\n"
            "| --- | ---: | ---: |\n"
            "| warm path | 18.4 | 42.7 |\n"
        )
    }

    candidates = measurement_candidates(output, command="profile throughput")

    assert "warm path 42.7 req/s" in candidates
