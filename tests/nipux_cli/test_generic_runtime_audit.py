from pathlib import Path


FORBIDDEN_RUNTIME_LITERALS = {
    "192.168",
    "9060 xt",
    "canadian",
    "client finder",
    "lead batch",
    "lead ledger",
    "client prospect",
    "edmonton",
    "home ssh",
    "home-ssh.local",
    "home@",
    "huggingface.co/qwen",
    "livebusiness",
    "qwen_qwen",
    "ssh home",
    "treefrog",
    "yelp",
    "llama.cpp",
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
