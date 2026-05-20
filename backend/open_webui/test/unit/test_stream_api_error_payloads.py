import pathlib
import sys


_BACKEND_DIR = pathlib.Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from open_webui.utils.middleware import _build_api_error_payload  # noqa: E402


def test_build_api_error_payload_uses_http_status_override_for_rate_limit():
    payload = _build_api_error_payload(
        (
            '{"error":{"message":"Request was rejected due to rate limiting. '
            'Details: TPM limit reached.","type":"bad_response_status_code",'
            '"param":"","code":"bad_response_status_code"}}'
        ),
        "cherryin-490b.agent/deepseek-v3.2(free)",
        status_override=429,
    )

    assert payload["type"] == "api_error"
    assert payload["model_id"] == "cherryin-490b.agent/deepseek-v3.2(free)"
    assert "HTTP 429" in payload["content"]
    assert "TPM limit reached" in payload["raw_message"]
    assert payload["reasons"] == ["api_rate_limit", "api_quota_exceeded"]
    assert payload["suggestion"] == "wait_retry"


def test_build_api_error_payload_handles_auth_failures_with_status_override():
    payload = _build_api_error_payload(
        '{"message":"invalid access token or token expired"}',
        "dashscope.qwen",
        status_override=401,
    )

    assert "HTTP 401" in payload["content"]
    assert "invalid access token or token expired" in payload["raw_message"]
    assert payload["reasons"] == ["api_auth_error"]
    assert payload["suggestion"] == "check_api_key"


def test_build_api_error_payload_marks_disconnected_response_as_possibly_billed():
    payload = _build_api_error_payload(
        "[ERROR: Server disconnected without sending a response.]",
        "gpt-image-2",
    )

    assert payload["family"] == "upstream_response_lost"
    assert payload["status"] is None
    assert "上游结果没有完整返回" in payload["title"]
    assert "可能已经在上游完成或产生计费" in payload["body"]
    assert payload["reasons"] == [
        "api_response_disconnected",
        "proxy_error",
        "possible_upstream_billed",
    ]
    assert payload["suggestion"] == "check_upstream_before_retry"
