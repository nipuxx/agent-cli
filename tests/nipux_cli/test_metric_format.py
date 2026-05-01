from nipux_cli.metric_format import format_metric_value


def test_format_metric_value_spaces_named_units():
    assert format_metric_value("citations", 42, "count") == "citations=42 count"
    assert format_metric_value("speed", 2.7, "tokens/s") == "speed=2.7 tokens/s"


def test_format_metric_value_keeps_attached_symbol_units():
    assert format_metric_value("accuracy", 98.2, "%") == "accuracy=98.2%"
    assert format_metric_value("throughput", 120, "/s") == "throughput=120/s"
