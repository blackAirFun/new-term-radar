import base64
import io
import json
import urllib.error
import urllib.request

import pytest

from radar import evidence


PROJECT = "https://github.com/acme/example"
README = f"{PROJECT}/blob/main/README.md"
CANDIDATE = {"url": PROJECT, "source": "github"}


def github_responses(monkeypatch, readme="# Example\n## Usage\nOpen the app.", **metadata):
    calls = []

    def request(url):
        calls.append(url)
        if url.endswith("/readme"):
            return {"content": base64.b64encode(readme.encode()).decode(), "encoding": "base64",
                    "html_url": README, "sha": "a" * 40}
        return {"html_url": PROJECT, "full_name": "acme/example", "description": "Example project",
                "private": False, "archived": False, "disabled": False, "stargazers_count": 12,
                **metadata}

    monkeypatch.setattr(evidence, "_request", request)
    return calls


def test_github_evidence_and_offline_cache(tmp_path, monkeypatch):
    calls = github_responses(
        monkeypatch,
        "[![badge](https://shields.io/test.svg)](https://example.com)\n# Example\n"
        + "Overview " * 140 + "\n## 使用方法\n运行示例脚本。",
    )
    value = evidence.collect(CANDIDATE, tmp_path)
    assert value["status"] == "verified"
    assert value["description"] == "Example project"
    assert value["evidence_urls"] == [PROJECT, README]
    assert value["readme_sha"] == "a" * 40
    assert value["stars"] == 12
    assert "运行示例脚本" in value["readme_excerpt"]
    assert "shields.io" not in value["readme_excerpt"]
    assert len(value["readme_excerpt"]) <= evidence.MAX_README

    # Different casing and a clone suffix identify the same GitHub repo.
    assert evidence.collect({"url": "https://github.com/Acme/EXAMPLE.git"}, tmp_path, offline=True) == value
    assert calls == [f"{evidence.API}/repos/acme/example", f"{evidence.API}/repos/acme/example/readme"]


@pytest.mark.parametrize("readme", [
    "# Example\n## Usage\n## Roadmap\nComing soon",
    "# Example\n```sh\n## Usage\nThis is code\n```",
    "# Example\n## Demo\n![screenshot](https://example.com/test.jpg)",
])
def test_readme_without_usage_is_partial(tmp_path, monkeypatch, readme):
    github_responses(monkeypatch, readme=readme)
    result = evidence.collect(CANDIDATE, tmp_path)
    assert result["status"] == "partial"
    assert not result["has_usage"]
    assert result["evidence_urls"] == [PROJECT, README]


@pytest.mark.parametrize("field", ["private", "archived", "disabled"])
def test_unavailable_repository_has_no_verified_readme(tmp_path, monkeypatch, field):
    calls = github_responses(monkeypatch, **{field: True})
    result = evidence.collect(CANDIDATE, tmp_path)
    assert result["status"] == "partial"
    assert not result["readme_excerpt"]
    assert len(calls) == 1
    if field == "private":
        assert not result["evidence_urls"]
        assert not result["description"]


def test_expired_cache_and_fetch_failure_do_not_preserve_verified(tmp_path, monkeypatch):
    github_responses(monkeypatch)
    assert evidence.collect(CANDIDATE, tmp_path)["status"] == "verified"
    cache = next((tmp_path / "evidence").glob("*.json"))
    entry = json.loads(cache.read_text())
    entry["stored_at"] -= evidence.CACHE_SECONDS + 1
    cache.write_text(json.dumps(entry))

    def fail(url):
        raise urllib.error.HTTPError(url, 403, "forbidden", None, None)

    monkeypatch.setattr(evidence, "_request", fail)
    assert evidence.collect(CANDIDATE, tmp_path, offline=True)["status"] == "partial"
    result = evidence.collect(CANDIDATE, tmp_path)
    assert result["status"] == "partial"
    assert not result["evidence_urls"]
    assert "HTTP 403" in result["reason"]


def test_readme_failure_preserves_checked_metadata(tmp_path, monkeypatch):
    github_responses(monkeypatch)
    request = evidence._request

    def fail_readme(url):
        if url.endswith("/readme"):
            raise TimeoutError
        return request(url)

    monkeypatch.setattr(evidence, "_request", fail_readme)
    value = evidence.collect(CANDIDATE, tmp_path)
    assert value["status"] == "partial"
    assert value["evidence_urls"] == [PROJECT]
    assert value["description"] == "Example project"


def test_rejects_readme_from_a_different_project(tmp_path, monkeypatch):
    github_responses(monkeypatch)
    request = evidence._request

    def wrong_readme(url):
        value = request(url)
        if url.endswith("/readme"):
            value["html_url"] = README.replace("/acme/", "/another-owner/")
        return value

    monkeypatch.setattr(evidence, "_request", wrong_readme)
    value = evidence.collect(CANDIDATE, tmp_path)
    assert value["status"] == "partial"
    assert value["evidence_urls"] == [PROJECT]


def test_cache_write_failure_keeps_verified_response(tmp_path, monkeypatch):
    (tmp_path / "evidence").touch()  # A file prevents creation of the cache directory.
    github_responses(monkeypatch)
    value = evidence.collect(CANDIDATE, tmp_path)
    assert value["status"] == "verified"
    assert value["evidence_urls"] == [PROJECT, README]


@pytest.mark.parametrize("url", [
    "https://github.com@example.org/acme/example", "https://github.com/acme/example/issues/1",
    "https://github.com/acme/..", "https://[broken", "http://github.com/acme/example",
])
def test_nonproject_or_unsafe_url_is_partial(tmp_path, monkeypatch, url):
    calls = github_responses(monkeypatch)
    value = evidence.collect({"url": url}, tmp_path)
    assert value["status"] == "partial"
    assert not calls


def test_github_request_uses_existing_token(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer test-token"
            assert request.full_url.startswith(f"{evidence.API}/repos/")
            assert timeout == evidence.TIMEOUT
            return io.BytesIO(b'{"key": "value"}')

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    assert evidence._request(f"{evidence.API}/repos/acme/example") == {"key": "value"}


@pytest.mark.parametrize(("code", "message", "allows_retry"), [
    (404, b"Not Found", True),
    (403, b"Resource not accessible by integration", True),
    (403, b"API rate limit exceeded", False),
])
def test_token_permission_error_can_retry_official_public_api(monkeypatch, code, message, allows_retry):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(request.get_header("Authorization"))
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, code, "", {}, io.BytesIO(message))
            return io.BytesIO(b'{"key": "value"}')

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: Opener())
    url = f"{evidence.API}/repos/acme/example"
    if allows_retry:
        assert evidence._request(url) == {"key": "value"}
        assert calls == ["Bearer test-token", None]
    else:
        with pytest.raises(urllib.error.HTTPError):
            evidence._request(url)
        assert calls == ["Bearer test-token"]


def test_github_request_rejects_cross_origin_redirect():
    req = urllib.request.Request(f"{evidence.API}/repos/acme/example",
                                 headers={"Authorization": "Bearer test-token"})
    with pytest.raises(urllib.error.HTTPError):
        evidence._GitHubRedirect().redirect_request(req, None, 301, "", {}, "https://example.org")
