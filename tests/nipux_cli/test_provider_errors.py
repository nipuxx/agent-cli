from nipux_cli.provider_errors import (
    provider_action_required,
    provider_action_required_note,
    provider_rate_limited,
)


class ProviderPayloadError(Exception):
    payload = {"error": {"message": "Key limit exceeded", "code": 403}}


def test_provider_action_required_detects_payload_and_status_text():
    assert provider_action_required(ProviderPayloadError("provider rejected request"))
    assert provider_action_required("PermissionDeniedError: Error code: 403")
    assert "operator action" in provider_action_required_note("invalid api key")


def test_provider_rate_limited_detects_transient_rate_text():
    assert provider_rate_limited("429 too many requests")
    assert provider_rate_limited("provider temporarily over capacity")
    assert not provider_rate_limited("invalid api key")
