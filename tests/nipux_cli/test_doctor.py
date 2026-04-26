from nipux_cli.config import AppConfig, RuntimeConfig
from nipux_cli.doctor import run_doctor


def test_doctor_checks_local_runtime_without_model_call(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(home=tmp_path))

    checks = run_doctor(config=config, check_model=False)

    assert {check.name for check in checks} == {"state_dir_writable", "sqlite", "tool_surface", "browser_runtime"}
    assert all(check.ok for check in checks)
