"""Image-to-prompt gateway contract: methods, canonical paths and body limits."""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "deploy/mini-api-gateway"
UUID = "00000000-0000-0000-0000-000000000001"


def test_prompt_routes_are_exact_and_check_original_uri():
    config = (GATE / "http.conf").read_text()
    assert "map $request_uri $mini_api_prompt_canonical {" in config
    raw = re.search(r"map \$request_uri \$mini_api_prompt_canonical \{(.*?)\n\}", config, re.S).group(1)
    canonical = [pattern for pattern in re.findall(r'"~(.*?)" 1;', raw)]
    allowed = re.findall(r'"~(.*?)" \$mini_api_prompt_canonical;', config)

    def permitted(method, path):
        return any(re.fullmatch(p, path) for p in canonical) and any(
            re.fullmatch(p, method + ":" + path.split("?")[0]) for p in allowed
        )

    for method, path in [
        ("POST", "/api/prompt-assets"),
        ("GET", f"/api/prompt-assets/{UUID}"),
        ("POST", "/api/prompt-analyses"),
        ("GET", "/api/prompt-analyses?limit=20"),
        ("GET", f"/api/prompt-analyses/{UUID}"),
        ("PATCH", f"/api/prompt-analyses/{UUID}"),
        ("DELETE", f"/api/prompt-analyses/{UUID}"),
        ("GET", "/api/prompt-examples"),
        ("POST", "/api/auth/wechat/image-to-prompt"),
    ]:
        assert permitted(method, "/mini-api" + path), (method, path)
    for method, path in [
        ("GET", "/api/prompt-assets"),
        ("DELETE", "/api/prompt-analyses"),
        ("POST", f"/api/prompt-analyses/{UUID}"),
        ("GET", "/api/prompt-assets/not-uuid"),
        ("GET", "/api/prompt-analyses/"),
        ("GET", "/api/prompt-analyses//"),
        ("GET", "/api/%70rompt-analyses"),
        ("GET", "/api/./prompt-analyses"),
        ("POST", "/api/submissions"),
        ("GET", "/api/admin/prompts"),
    ]:
        assert not permitted(method, "/mini-api" + path), (method, path)


def test_upload_has_separate_limit_and_json_gateway_errors():
    config = (GATE / "server.conf").read_text()
    upload = config.split("location = /mini-api/api/prompt-assets {", 1)[1].split("\n}\n", 1)[0]
    assert "client_max_body_size 6m;" in upload
    assert "Authorization $http_authorization" in upload
    assert "error_page 413" in upload
    assert "application/json" in config and "PAYLOAD_TOO_LARGE" in config
    assert "Idempotency-Key $http_idempotency_key" in config


def test_reviewed_profile_gateway_can_be_upgraded_and_rolled_back():
    spec = importlib.util.spec_from_file_location("prompt_gateway_release", GATE / "release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    fixtures = ROOT / "tests/fixtures"
    old_http = (fixtures / "mini_api_profile_http.conf").read_text()
    old_server = (fixtures / "mini_api_profile_server.conf").read_text()
    original = "server { server_name aigc-prompt.com; location / { proxy_pass http://frontend; } }\n"
    end = release.domain_block(original)[1]
    prior = (
        release.HTTP_BEGIN
        + old_http
        + release.HTTP_END
        + original[:end]
        + release.SERVER_BEGIN
        + old_server
        + release.SERVER_END
        + original[end:]
    )
    candidate = release.merge(prior)
    assert "/api/prompt-analyses" in candidate
    assert release.remove(candidate) == original
