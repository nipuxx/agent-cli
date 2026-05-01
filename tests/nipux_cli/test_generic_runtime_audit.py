from pathlib import Path


FORBIDDEN_RUNTIME_LITERALS = {
    "home-ssh.local",
    "treefrog",
    "client finder",
    "livebusiness",
    "yelp",
    "9060 xt",
}


def test_runtime_code_has_no_task_specific_literals():
    root = Path(__file__).resolve().parents[2] / "nipux_cli"
    haystack = "\n".join(
        path.read_text(encoding="utf-8", errors="replace").lower()
        for path in sorted(root.glob("*.py"))
        if path.name != "__init__.py"
    )

    for literal in FORBIDDEN_RUNTIME_LITERALS:
        assert literal not in haystack
