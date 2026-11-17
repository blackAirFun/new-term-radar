"""Check public GitHub documentation and cache the sources for editorial review."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.parse import urlsplit

from . import UA

API = "https://api.github.com"
CACHE_SECONDS = 12 * 3600
MAX_RESPONSE = 1024 * 1024
MAX_README = 2600
TIMEOUT = 8
_CACHE_VERSION = 1
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
_HEADING = re.compile(r"(?m)^ {0,3}#{1,6}[ \t]+([^\n]+)")
_USAGE = re.compile(
    r"^(?:usage|how to (?:use|install)|(?:quick|getting) start(?:ed)?|"
    r"install(?:ation|ing)?|setup|demo|download)\b|^(?:使用|安装|快速开始|上手|快速入门|体验)",
    re.I,
)
_FEATURES = re.compile(r"^(?:features?|requirements?|prerequisites?)\b|^(?:功能|环境要求|系统要求)", re.I)
_FENCED = re.compile(r"(?ms)^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*$|\Z)")


def _github_path(url: object) -> str:
    if not isinstance(url, str) or re.search(r"[\s\x00-\x1f\x7f]", url):
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return ""
    return parsed.path.removeprefix("/").rstrip("/")


def _repo(path: str) -> str:
    if _REPO.fullmatch(path) and path.rsplit("/", 1)[1] not in {".", ".."}:
        return path
    return ""


class _GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.netloc != "api.github.com":
            raise urllib.error.HTTPError(req.full_url, code, "GitHub API redirect changed origin", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(url: str) -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(_GitHubRedirect())
    try:
        return _read_response(opener, request)
    except urllib.error.HTTPError as exc:
        # Actions tokens are repository-scoped. A public repo can still be read anonymously.
        if not token or not _permission_denied(exc):
            raise
        exc.close()
        request.remove_header("Authorization")
        return _read_response(opener, request)


def _permission_denied(exc: urllib.error.HTTPError) -> bool:
    return exc.code == 404 or (
        exc.code == 403 and exc.fp is not None
        and b"resource not accessible by integration" in exc.read(4096).lower()
    )


def _read_response(opener, request: urllib.request.Request) -> dict:
    with opener.open(request, timeout=TIMEOUT) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError("GitHub response exceeds evidence size limit")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("GitHub response is not a JSON object")
    return value


def _readme_evidence(markdown: str) -> tuple[str, bool]:
    # Keep commands inside code fences, but only use headings outside fences as section markers.
    text = re.sub(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\)", "", markdown)
    text = re.sub(r"<!--.*?-->|<[^>\n]+>", " ", text, flags=re.S)
    headings = list(_HEADING.finditer(_FENCED.sub(lambda m: m[0].replace("#", " "), text)))
    usage, features = [], []
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        if not any(ch.isalnum() for ch in text[match.end():end]):
            continue
        title = re.sub(r"^\W+", "", match[1])
        section = text[match.start():end].strip()
        if _USAGE.match(title):
            usage.append(section)
        elif _FEATURES.match(title):
            features.append(section)
    intro = text.strip()[:1000].strip()
    parts = [intro]
    for section in usage[:1] + features[:1]:
        if section not in intro:
            parts.append(section[:780].strip())
    return "\n\n".join(parts)[:MAX_README], bool(usage)


def _read_cache(path: Path, repo: str) -> dict | None:
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(entry["stored_at"])
        value = entry["value"]
        if (entry["version"] == _CACHE_VERSION and entry["repo"] == repo
                and 0 <= age < CACHE_SECONDS and isinstance(value, dict)
                and value.get("status") in {"verified", "partial"} and value.get("checked_at")):
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _write_cache(path: Path, repo: str, value: dict) -> None:
    entry = {"version": _CACHE_VERSION, "repo": repo, "stored_at": time.time(), "value": value}
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temp = Path(stream.name)
    try:
        with stream:
            json.dump(entry, stream, ensure_ascii=False)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def collect(candidate: dict, state: Path, offline: bool = False) -> dict:
    """Verify a public repo and its usage documentation without executing project code."""
    url = candidate.get("url")
    value = {"status": "partial", "checked_at": datetime.now(timezone.utc).isoformat(),
             "source": url if isinstance(url, str) else None, "description": None,
             "readme_excerpt": "", "has_usage": False, "evidence_urls": [],
             "reason": "仅支持自动核实 GitHub 仓库，其他来源需人工审核"}
    repo = _repo(_github_path(url).removesuffix(".git"))
    raw_evidence = candidate.get("evidence")
    if not url and candidate.get("source") == "github" and isinstance(raw_evidence, dict):
        repo = _repo(str(raw_evidence.get("repo") or ""))
    if not repo:
        return value

    identity = repo.lower()
    cache = state / "evidence" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
    cached = _read_cache(cache, identity)
    if cached is not None:
        return cached
    if offline:
        return {**value, "reason": "离线模式下没有有效证据缓存"}

    try:
        _fetch(repo, value)
    except urllib.error.HTTPError as exc:
        exc.close()
        return {**value, "reason": f"项目资料获取失败（HTTP {exc.code}）"}
    except (OSError, HTTPException, ValueError, TypeError):
        return {**value, "reason": "项目资料获取失败或响应格式异常"}

    try:
        _write_cache(cache, identity, value)
    except OSError:
        pass  # A valid response remains valid if the state directory cannot be written.
    return value


def _fetch(repo: str, value: dict) -> None:
    metadata = _request(f"{API}/repos/{repo}")
    canonical = _repo(_github_path(metadata.get("html_url")))
    if not canonical or canonical.lower() != str(metadata.get("full_name", "")).lower():
        raise ValueError("GitHub repo response has no matching identity")
    if metadata.get("private") is not False or metadata.get("visibility", "public") != "public":
        value["reason"] = "仓库公开状态未得到核实"
        return
    source = f"https://github.com/{canonical}"
    description = metadata.get("description")
    value.update(source=source, evidence_urls=[source],
                 description=description.strip()[:500] or None if isinstance(description, str) else None,
                 stars=metadata.get("stargazers_count"),
                 repository_archived=bool(metadata.get("archived")),
                 repository_disabled=bool(metadata.get("disabled")))
    if value["repository_archived"] or value["repository_disabled"]:
        value["reason"] = "仓库已归档或停用"
        return

    readme = _request(f"{API}/repos/{canonical}/readme")
    readme_url = readme.get("html_url")
    if (not _github_path(readme_url).lower().startswith(f"{canonical.lower()}/blob/")
            or readme.get("encoding") != "base64" or not isinstance(readme.get("content"), str)):
        raise ValueError("GitHub README response has no matching source or content")
    content = base64.b64decode("".join(readme["content"].split()), validate=True).decode("utf-8-sig")
    excerpt, has_usage = _readme_evidence(content)
    value.update(evidence_urls=[source, readme_url], readme_excerpt=excerpt, has_usage=has_usage,
                 readme_sha=readme.get("sha"))
    if has_usage:
        value.update(status="verified", reason="已核实项目仓库及 README 使用说明")
    else:
        value["reason"] = "README 未提供明确的使用说明"
