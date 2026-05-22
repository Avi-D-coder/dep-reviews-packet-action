#!/usr/bin/env python3
"""Install Reviews CLI and configure it for the action run."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


DEFAULT_REVIEWS_REPO = "figitaki/reviews"


def main() -> int:
    args = parse_args()
    runner_temp = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    workdir = Path(args.workdir)
    wrapper_bin = workdir / "bin"
    install_dir = Path(args.install_dir)
    reviews_home = runner_temp / "dep-review-reviews-home"

    install_reviews_cli(args.version, install_dir, args.repo)
    write_reviews_config(reviews_home, args.server_url, args.api_key)
    write_wrapper(wrapper_bin, install_dir / "reviews", reviews_home)
    add_path(wrapper_bin)
    write_outputs({"reviews-command": str(wrapper_bin / "reviews")})

    print(f"Configured Reviews CLI wrapper at {wrapper_bin / 'reviews'}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", default=".dep-review-work")
    parser.add_argument("--server-url", default=os.environ.get("INPUT_REVIEWS_SERVER_URL", "https://reviews-dev.fly.dev"))
    parser.add_argument("--api-key", default=os.environ.get("INPUT_REVIEWS_API_KEY", ""))
    parser.add_argument(
        "--version",
        default=os.environ.get("INPUT_REVIEWS_CLI_VERSION") or os.environ.get("REVIEWS_VERSION") or "0.0.1-alpha.0",
    )
    parser.add_argument("--repo", default=os.environ.get("REVIEWS_REPO", DEFAULT_REVIEWS_REPO))
    parser.add_argument(
        "--install-dir",
        default=os.environ.get("INSTALL_DIR")
        or str(Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "dep-review-reviews-bin"),
    )
    args = parser.parse_args()
    if not args.api_key:
        raise SystemExit("Reviews API key is required")
    return args


def install_reviews_cli(version: str, install_dir: Path, repo: str = DEFAULT_REVIEWS_REPO) -> None:
    install_dir.mkdir(parents=True, exist_ok=True)
    target = platform_target()
    release = fetch_release(version, repo)
    asset_url, asset_name, checksum_url = select_release_assets(release, target)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / asset_name
        checksums = tmp_path / "checksums.txt"
        extract_dir = tmp_path / "extract"
        extract_dir.mkdir()

        download(asset_url, archive)
        download(checksum_url, checksums)
        expected = checksum_for_asset(checksums.read_text(encoding="utf-8"), asset_name)
        if not expected:
            raise RuntimeError(f"checksums.txt did not include {asset_name}")
        actual = sha256_file(archive)
        if actual != expected:
            raise RuntimeError(f"checksum verification failed for {asset_name}: expected {expected}, got {actual}")

        safe_extract_tar(archive, extract_dir)
        extracted_reviews = extract_dir / "reviews"
        if not extracted_reviews.is_file():
            raise RuntimeError(f"release archive did not contain a reviews binary at {extracted_reviews}")
        reviews = install_dir / "reviews"
        shutil.copy2(extracted_reviews, reviews)
        reviews.chmod(0o755)

    reviews = install_dir / "reviews"
    if not reviews.is_file():
        raise RuntimeError(f"Reviews install did not create {reviews}")


def release_tag_for_version(version: str) -> str:
    version = version.strip()
    if not version:
        raise RuntimeError("Reviews CLI version is required")
    if version.startswith("cli-v"):
        return version
    if version.startswith("v"):
        return f"cli-{version}"
    return f"cli-v{version}"


def release_api_url(version: str, repo: str) -> str:
    repo = repo.strip() or DEFAULT_REVIEWS_REPO
    if version.strip():
        tag = urllib.parse.quote(release_tag_for_version(version), safe="")
        return f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    return f"https://api.github.com/repos/{repo}/releases"


def fetch_release(version: str, repo: str = DEFAULT_REVIEWS_REPO) -> dict[str, Any]:
    override = os.environ.get("REVIEWS_RELEASES_API_URL")
    if override:
        url = override
    else:
        url = release_api_url(version, repo)

    payload = request_bytes(url)
    data = json.loads(payload.decode("utf-8"))
    if isinstance(data, list):
        if not data:
            raise RuntimeError("Reviews releases API returned no releases")
        return data[0]
    return data


def select_release_assets(release: dict[str, Any], target: str) -> tuple[str, str, str]:
    archive: Optional[dict[str, Any]] = None
    checksums: Optional[dict[str, Any]] = None
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(f"-{target}.tar.gz") and name.startswith("reviews-cli-"):
            archive = asset
        elif name == "checksums.txt":
            checksums = asset

    if not archive:
        raise RuntimeError(f"no Reviews CLI release asset found for {target}")
    if not checksums:
        raise RuntimeError("Reviews CLI release did not include checksums.txt")

    archive_url = str(archive.get("browser_download_url", ""))
    checksum_url = str(checksums.get("browser_download_url", ""))
    if not archive_url or not checksum_url:
        raise RuntimeError("Reviews CLI release asset was missing a browser_download_url")
    return archive_url, str(archive["name"]), checksum_url


def platform_target(system: Optional[str] = None, machine: Optional[str] = None) -> str:
    system = system or os.uname().sysname
    machine = machine or os.uname().machine
    normalized = machine.lower()
    if system == "Darwin" and normalized == "arm64":
        return "macos-arm64"
    if system == "Darwin" and normalized == "x86_64":
        return "macos-x64"
    if system == "Linux" and normalized in {"aarch64", "arm64"}:
        return "linux-arm64"
    if system == "Linux" and normalized in {"x86_64", "amd64"}:
        return "linux-x64"
    raise RuntimeError(f"unsupported platform: {system} {machine}")


def download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(request(url)) as response, open(dest, "wb") as fh:
        shutil.copyfileobj(response, fh)


def request_bytes(url: str) -> bytes:
    try:
        with urllib.request.urlopen(request(url)) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Reviews release request failed: {exc.code} {detail}") from exc


def request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "dep-reviews-packet-action",
        },
    )


def checksum_for_asset(checksums_body: str, asset_name: str) -> str:
    for line in checksums_body.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == asset_name:
            return parts[0]
    return ""


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract_tar(archive: Path, dest: Path) -> None:
    dest_resolved = dest.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            member_path = dest_resolved / member.name
            if not is_relative_to(member_path.resolve(), dest_resolved):
                raise RuntimeError(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk():
                target = Path(member.linkname)
                if target.is_absolute() or ".." in target.parts:
                    raise RuntimeError(f"unsafe link in archive: {member.name} -> {member.linkname}")
        tar.extractall(dest)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def write_reviews_config(home: Path, server_url: str, api_key: str) -> None:
    config_dir = home / ".config" / "reviews"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = config_dir / "config.toml"
    config.write_text(
        f"[default]\nserver_url = {toml_string(server_url)}\napi_token = {toml_string(api_key)}\n",
        encoding="utf-8",
    )
    config.chmod(stat.S_IRUSR | stat.S_IWUSR)


def write_wrapper(wrapper_bin: Path, reviews_bin: Path, reviews_home: Path) -> None:
    wrapper_bin.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_bin / "reviews"
    wrapper.write_text(
        "#!/usr/bin/env sh\n"
        "set -eu\n"
        f"export HOME={shell_quote(str(reviews_home))}\n"
        f"exec {shell_quote(str(reviews_bin))} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)


def add_path(path: Path) -> None:
    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with open(github_path, "a", encoding="utf-8") as fh:
            fh.write(str(path) + "\n")


def write_outputs(outputs: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as fh:
        for name, value in outputs.items():
            if "\n" in value:
                raise RuntimeError(f"output {name} cannot contain newlines")
            fh.write(f"{name}={value}\n")


def toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
