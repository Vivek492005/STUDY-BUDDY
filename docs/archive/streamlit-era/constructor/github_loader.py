import os
import base64
import requests
from urllib.parse import urlparse
from datetime import datetime


def parse_repo(url: str):
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    p = urlparse(url)
    if p.netloc != "github.com":
        return None, None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1]


def _headers():
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    return headers


def _check_rate_limit(headers):
    """Check current GitHub API rate limit status"""
    try:
        r = requests.get("https://api.github.com/rate_limit", headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            core = data.get("resources", {}).get("core", {})
            remaining = core.get("remaining", 0)
            limit = core.get("limit", 0)
            reset = core.get("reset", 0)
            
            if remaining < 10:
                reset_time = datetime.fromtimestamp(reset).strftime("%Y-%m-%d %H:%M:%S")
                return {
                    "status": "low",
                    "remaining": remaining,
                    "limit": limit,
                    "reset_time": reset_time
                }
            return {
                "status": "ok",
                "remaining": remaining,
                "limit": limit,
                "reset_time": None
            }
    except Exception:
        pass
    return None


def fetch_repo(owner: str, repo: str, max_files: int = 10):
    api = f"https://api.github.com/repos/{owner}/{repo}"
    headers = _headers()
    
    # Check rate limit before starting
    rate_limit = _check_rate_limit(headers)
    if rate_limit and rate_limit["status"] == "low":
        raise RuntimeError(
            f"GitHub API rate limit is low ({rate_limit['remaining']}/{rate_limit['limit']}). "
            f"Resets at {rate_limit['reset_time']}. "
            f"Please wait or add a GITHUB_TOKEN to your .env file."
        )

    # --- Repo metadata ---
    try:
        r = requests.get(api, headers=headers, timeout=10)
    except requests.exceptions.Timeout:
        raise RuntimeError("GitHub API request timed out. Try again later.")
    except requests.exceptions.ConnectionError:
        raise RuntimeError("Failed to connect to GitHub API. Check your internet connection.")
    
    if r.status_code == 401:
        raise RuntimeError(
            "GitHub API returned 401 Unauthorized. "
            "Your GITHUB_TOKEN may be invalid or expired. "
            "Generate a new token at: https://github.com/settings/tokens"
        )
    
    if r.status_code == 403:
        # Check if it's rate limit
        rate_limit_info = r.headers.get("X-RateLimit-Remaining", "0")
        if rate_limit_info == "0":
            reset_time = datetime.fromtimestamp(int(r.headers.get("X-RateLimit-Reset", 0))).strftime("%Y-%m-%d %H:%M:%S")
            raise RuntimeError(
                f"GitHub API rate limit exceeded. "
                f"Resets at {reset_time}. "
                f"Add a GITHUB_TOKEN to your .env file for higher limits."
            )
        else:
            raise RuntimeError("GitHub API returned 403 Forbidden. Check repository access.")
    
    if r.status_code == 404:
        raise RuntimeError("Repository not found. Check owner/repo name.")
    
    if r.status_code != 200:
        raise RuntimeError(f"GitHub API error: {r.status_code} - {r.reason}")
    
    meta = r.json()

    # --- README ---
    readme = ""
    try:
        r = requests.get(f"{api}/readme", headers=headers, timeout=10)
        if r.status_code == 200:
            readme = base64.b64decode(
                r.json().get("content", "")
            ).decode("utf-8", "ignore")
    except Exception:
        pass  # README is optional

    # --- Repo tree ---
    branch = meta.get("default_branch", "main")
    try:
        tree_resp = requests.get(
            f"{api}/git/trees/{branch}?recursive=1",
            headers=headers,
            timeout=15,
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("GitHub tree request timed out. Try reducing max_files.")
    
    if tree_resp.status_code == 404:
        raise RuntimeError(f"Repository branch '{branch}' not found.")
    
    if tree_resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch repository tree: {tree_resp.status_code}")

    tree = tree_resp.json()

    files = []
    for item in tree.get("tree", []):
        if len(files) >= max_files:
            break
        if item.get("type") == "blob" and item.get("size", 0) <= 200_000:
            files.append(item["path"])

    # --- File contents ---
    contents = []
    for path in files:
        try:
            r = requests.get(f"{api}/contents/{path}", headers=headers, timeout=10)
            if r.status_code == 200 and "content" in r.json():
                try:
                    text = base64.b64decode(
                        r.json()["content"]
                    ).decode("utf-8", "ignore")
                    contents.append({"path": path, "content": text})
                except Exception:
                    pass
        except requests.exceptions.Timeout:
            continue  # Skip file if timeout
        except Exception:
            continue  # Skip file if error

    # --- Languages ---
    languages = {}
    try:
        langs = requests.get(f"{api}/languages", headers=headers, timeout=10)
        languages = langs.json() if langs.status_code == 200 else {}
    except Exception:
        pass

    return {
        "name": meta.get("name"),
        "description": meta.get("description"),
        "languages": languages,
        "readme": readme,
        "files": contents,
    }
