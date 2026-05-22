#!/usr/bin/env python3
"""Prepare synthetic dependency source diffs for Cargo.lock updates."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - GitHub runners provide Python 3.11+.
    tomllib = None  # type: ignore[assignment]


WORKDIR = Path(".dep-review-work")


@dataclasses.dataclass(frozen=True)
class Package:
    name: str
    version: str
    source: Optional[str]
    checksum: Optional[str] = None

    @property
    def is_external(self) -> bool:
        return bool(self.source)

    @property
    def source_kind(self) -> str:
        return source_kind(self.source)

    @property
    def source_key(self) -> str:
        return normalize_source(self.source)


@dataclasses.dataclass(frozen=True)
class Change:
    old: Optional[Package]
    new: Package
    key: str
    change_kind: str


def main() -> int:
    args = parse_args()
    workspace = Path.cwd()
    workdir = workspace / args.workdir
    reset_workdir(workdir)

    manifest: dict[str, Any] = {
        "format_version": 1,
        "workspace_path": str(workspace),
        "workdir": str(workdir),
        "results_path": str(workdir / "results.json"),
        "lockfile": args.lockfile,
        "base_ref": "",
        "head_ref": "",
        "dependencies": [],
        "skipped": [],
        "artifacts": [],
    }

    try:
        base_ref, head_ref = infer_refs(args.base_ref, args.head_ref)
        manifest["base_ref"] = base_ref
        manifest["head_ref"] = head_ref

        base_lock = git_show(base_ref, args.lockfile)
        head_lock = git_show(head_ref, args.lockfile)
        changes, skipped = pair_changes(parse_lock(base_lock), parse_lock(head_lock))
        manifest["skipped"].extend(dataclasses.asdict(item) for item in skipped)

        for change in changes:
            try:
                dep = prepare_change(change, workdir, base_ref, head_ref, args.lockfile)
                manifest["dependencies"].append(dep)
                if dep.get("artifact"):
                    manifest["artifacts"].append(dep["artifact"])
            except Exception as exc:  # Continue with other dependencies.
                manifest["skipped"].append(
                    {
                        "name": change.new.name,
                        "old_version": change.old.version if change.old else None,
                        "new_version": change.new.version,
                        "source": change.new.source,
                        "reason": f"materialization failed: {exc}",
                    }
                )
    except Exception as exc:
        write_manifest(workdir, manifest)
        raise SystemExit(f"prepare failed: {exc}") from exc

    write_manifest(workdir, manifest)
    write_outputs(
        {
            "dependency-count": str(len(manifest["dependencies"])),
            "artifact-count": str(len(manifest["artifacts"])),
            "skipped-count": str(len(manifest["skipped"])),
            "manifest-path": str(workdir / "manifest.json"),
        }
    )
    print(
        f"Prepared {len(manifest['dependencies'])} dependency diff(s); "
        f"skipped {len(manifest['skipped'])} package change(s)."
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lockfile", default=os.environ.get("INPUT_LOCKFILE", "Cargo.lock"))
    parser.add_argument("--base-ref", default=os.environ.get("INPUT_BASE_REF", ""))
    parser.add_argument("--head-ref", default=os.environ.get("INPUT_HEAD_REF", ""))
    parser.add_argument("--workdir", default=str(WORKDIR))
    return parser.parse_args()


def reset_workdir(workdir: Path) -> None:
    if workdir.exists():
        shutil.rmtree(workdir)
    (workdir / "deps").mkdir(parents=True, exist_ok=True)
    (workdir / "artifacts").mkdir(parents=True, exist_ok=True)


def infer_refs(base_ref: Optional[str], head_ref: Optional[str]) -> tuple[str, str]:
    base_ref = (base_ref or "").strip()
    head_ref = (head_ref or "").strip()
    if base_ref and head_ref:
        return base_ref, head_ref

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).is_file():
        with open(event_path, "r", encoding="utf-8") as fh:
            event = json.load(fh)
        pr = event.get("pull_request")
        if pr:
            return (
                base_ref or pr["base"]["sha"],
                head_ref or pr["head"]["sha"],
            )

    return base_ref or "HEAD~1", head_ref or "HEAD"


def git_show(ref: str, path: str) -> str:
    return run(["git", "show", f"{ref}:{path}"]).stdout


def parse_lock(body: str) -> list[Package]:
    data = tomllib.loads(body) if tomllib is not None else parse_lock_minimal(body)
    packages = []
    for item in data.get("package", []):
        packages.append(
            Package(
                name=str(item["name"]),
                version=str(item["version"]),
                source=item.get("source"),
                checksum=item.get("checksum"),
            )
        )
    return packages


def parse_lock_minimal(body: str) -> dict[str, Any]:
    packages: list[dict[str, str]] = []
    current: Optional[dict[str, str]] = None
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[[package]]":
            current = {}
            packages.append(current)
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in {"name", "version", "source", "checksum"}:
            current[key] = parse_toml_string(value.strip())
    return {"package": packages}


def parse_toml_string(value: str) -> str:
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return bytes(value[1:-1], "utf-8").decode("unicode_escape")
    return value


@dataclasses.dataclass(frozen=True)
class Skipped:
    name: str
    old_version: Optional[str]
    new_version: Optional[str]
    source: Optional[str]
    reason: str


def pair_changes(old_packages: list[Package], new_packages: list[Package]) -> tuple[list[Change], list[Skipped]]:
    old_index = index_external(old_packages)
    new_index = index_external(new_packages)
    changes: list[Change] = []
    skipped: list[Skipped] = []
    consumed_old: set[int] = set()
    consumed_new: set[int] = set()

    for key in sorted(set(old_index) | set(new_index)):
        old_items = old_index.get(key, [])
        new_items = new_index.get(key, [])
        name = (old_items or new_items)[0].name
        source = (new_items or old_items)[0].source

        if len(old_items) > 1 or len(new_items) > 1:
            skipped.append(Skipped(name, None, None, source, "ambiguous multi-version dependency"))
            consumed_old.update(id(item) for item in old_items)
            consumed_new.update(id(item) for item in new_items)
            continue
        if not old_items or not new_items:
            continue
        old = old_items[0]
        new = new_items[0]
        consumed_old.add(id(old))
        consumed_new.add(id(new))
        if old.version == new.version and old.source == new.source and old.checksum == new.checksum:
            continue
        changes.append(Change(old=old, new=new, key=key, change_kind="version-update"))

    unmatched_old = [package for package in external_packages(old_packages) if id(package) not in consumed_old]
    unmatched_new = [package for package in external_packages(new_packages) if id(package) not in consumed_new]
    old_by_name = group_by_name(unmatched_old)
    new_by_name = group_by_name(unmatched_new)

    for name in sorted(set(old_by_name) | set(new_by_name)):
        old_items = old_by_name.get(name, [])
        new_items = new_by_name.get(name, [])
        source = (new_items or old_items)[0].source

        if len(old_items) == 1 and len(new_items) == 1:
            old = old_items[0]
            new = new_items[0]
            changes.append(
                Change(
                    old=old,
                    new=new,
                    key=f"{name}|{old.source_key}|{new.source_key}",
                    change_kind="source-migration",
                )
            )
            continue
        if not old_items and len(new_items) == 1:
            new = new_items[0]
            changes.append(
                Change(
                    old=None,
                    new=new,
                    key=f"{name}|added|{new.source_key}",
                    change_kind="added",
                )
            )
            continue
        if len(old_items) == 1 and not new_items:
            old = old_items[0]
            skipped.append(Skipped(name, old.version, None, source, "removed dependency"))
            continue

        old_version = old_items[0].version if len(old_items) == 1 else None
        new_version = new_items[0].version if len(new_items) == 1 else None
        skipped.append(Skipped(name, old_version, new_version, source, "ambiguous multi-version dependency"))

    return changes, skipped


def index_external(packages: Iterable[Package]) -> dict[str, list[Package]]:
    index: dict[str, list[Package]] = {}
    for package in external_packages(packages):
        key = f"{package.name}|{package.source_key}"
        index.setdefault(key, []).append(package)
    return index


def external_packages(packages: Iterable[Package]) -> list[Package]:
    return [package for package in packages if package.is_external]


def group_by_name(packages: Iterable[Package]) -> dict[str, list[Package]]:
    grouped: dict[str, list[Package]] = {}
    for package in packages:
        grouped.setdefault(package.name, []).append(package)
    return grouped


def source_kind(source: Optional[str]) -> str:
    if not source:
        return "path"
    if source.startswith(("registry+", "sparse+")) and is_crates_io_source(source):
        return "crates.io"
    if source.startswith("git+"):
        return "git"
    if source.startswith(("registry+", "sparse+")):
        return "registry"
    return "unknown"


def normalize_source(source: Optional[str]) -> str:
    if not source:
        return "path"
    if source.startswith(("registry+", "sparse+")):
        if is_crates_io_source(source):
            return "registry:crates-io"
        _, url = source.split("+", 1)
        return f"registry:{normalize_url(url)}"
    if source.startswith("git+"):
        info = parse_git_source(source)
        return f"git:{normalize_url(info['url'])}"
    return source


def is_crates_io_source(source: str) -> bool:
    return "github.com/rust-lang/crates.io-index" in source or "index.crates.io" in source


def normalize_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))


def parse_git_source(source: str) -> dict[str, str]:
    value = source.removeprefix("git+")
    before_fragment, _, rev = value.partition("#")
    parsed = urllib.parse.urlsplit(before_fragment)
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    params = urllib.parse.parse_qs(parsed.query)
    return {
        "url": url,
        "rev": rev,
        "query": parsed.query,
        "branch": first(params.get("branch")),
        "tag": first(params.get("tag")),
        "requested_rev": first(params.get("rev")),
    }


def first(values: Optional[list[str]]) -> str:
    return values[0] if values else ""


def prepare_change(change: Change, workdir: Path, base_ref: str, head_ref: str, lockfile: str) -> dict[str, Any]:
    slug = slug_for(change)
    dep_dir = workdir / "deps" / slug
    old_src = dep_dir / "old-src"
    new_src = dep_dir / "new-src"
    repo = dep_dir / "repo"
    dep_dir.mkdir(parents=True, exist_ok=True)

    if change.old:
        old_info = materialize_package(change.old, old_src, workdir, base_ref, "old", lockfile)
    else:
        old_info = {"method": "empty"}
    new_info = materialize_package(change.new, new_src, workdir, head_ref, "new", lockfile)

    artifact_path = create_source_artifact(new_src, workdir / "artifacts", slug)
    repo_created = create_synthetic_repo(change, old_src, new_src, repo)
    if not repo_created:
        raise RuntimeError("old and new dependency sources produced no diff")

    raw_diff = run(["git", "diff", "--unified=3", "HEAD~1..HEAD"], cwd=repo).stdout
    hunks = parse_diff_hunks(raw_diff)
    hunks_path = dep_dir / "hunks.json"
    packet_path = dep_dir / "packet.md"
    write_json(hunks_path, {"hunks": hunks})
    packet_path.write_text(packet_skeleton(change, hunks), encoding="utf-8")

    local_checkout = local_checkout_command(change.new, slug, artifact_path.name)
    metadata = {
        "slug": slug,
        "name": change.new.name,
        "change_kind": change.change_kind,
        "change_label": change_label(change),
        "old_version": change.old.version if change.old else None,
        "new_version": change.new.version,
        "source_kind": change.new.source_kind,
        "old_source_kind": change.old.source_kind if change.old else None,
        "new_source_kind": change.new.source_kind,
        "old_source": change.old.source if change.old else None,
        "new_source": change.new.source,
        "old_materialized_from": old_info,
        "new_materialized_from": new_info,
        "repo_path": str(repo),
        "packet_path": str(packet_path),
        "hunks_path": str(hunks_path),
        "artifact": {
            "name": artifact_name(),
            "file": str(artifact_path),
            "tarball": artifact_path.name,
        },
        "local_checkout": {
            "command": local_checkout,
        },
    }
    write_json(dep_dir / "metadata.json", metadata)
    return metadata


def slug_for(change: Change) -> str:
    if change.old:
        base = f"{change.new.name}-{change.old.version}-to-{change.new.version}"
    else:
        base = f"{change.new.name}-added-{change.new.version}"
    digest = hashlib.sha256(change.key.encode("utf-8")).hexdigest()[:8]
    return f"{slugify(base)}-{digest}"


def change_label(change: Change) -> str:
    if change.change_kind == "added":
        return f"added {change.new.version}"
    if change.change_kind == "source-migration" and change.old:
        return (
            f"{change.old.version} ({change.old.source_kind}) -> "
            f"{change.new.version} ({change.new.source_kind})"
        )
    if change.old:
        return f"{change.old.version} -> {change.new.version}"
    return f"added {change.new.version}"


def slugify(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:120] or "dependency"


def materialize_package(
    package: Package,
    dest: Path,
    workdir: Path,
    ref: str,
    role: str,
    lockfile: str,
) -> dict[str, Any]:
    fixture = fixture_source(package)
    if fixture:
        copy_tree_contents(fixture, dest)
        return {"method": "fixture", "path": str(fixture)}

    if package.source_kind == "crates.io":
        archive = download_crate(package, workdir / "downloads")
        extract_crate(archive, dest)
        return {"method": "crates.io", "archive": str(archive)}

    if package.source_kind == "git":
        info = parse_git_source(package.source or "")
        clone_git_source(info, dest)
        return {"method": "git", **info}

    vendor_source(package, dest, workdir, ref, role, lockfile)
    return {"method": "cargo-vendor", "ref": ref}


def fixture_source(package: Package) -> Optional[Path]:
    root = os.environ.get("DEP_REVIEWS_FIXTURE_ROOT")
    if not root:
        return None
    candidates = [
        Path(root) / package.name / package.version,
        Path(root) / f"{package.name}-{package.version}",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def download_crate(package: Package, download_dir: Path) -> Path:
    download_dir.mkdir(parents=True, exist_ok=True)
    archive = download_dir / f"{package.name}-{package.version}.crate"
    url = crates_download_url(package.name, package.version)
    request = urllib.request.Request(url, headers={"User-Agent": "dep-reviews-packet-action"})
    with urllib.request.urlopen(request) as response, open(archive, "wb") as fh:
        shutil.copyfileobj(response, fh)

    if package.checksum:
        actual = sha256_file(archive)
        if actual != package.checksum:
            raise RuntimeError(
                f"checksum mismatch for {package.name} {package.version}: "
                f"expected {package.checksum}, got {actual}"
            )
    return archive


def crates_download_url(name: str, version: str) -> str:
    return f"https://crates.io/api/v1/crates/{urllib.parse.quote(name)}/{urllib.parse.quote(version)}/download"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_crate(archive: Path, dest: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        safe_extract_tar(archive, tmp_path)
        roots = [path for path in tmp_path.iterdir() if path.is_dir()]
        source_root = roots[0] if len(roots) == 1 else tmp_path
        copy_tree_contents(source_root, dest)


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


def clone_git_source(info: dict[str, str], dest: Path) -> None:
    clone_dir = dest.with_name(f"{dest.name}-clone")
    if clone_dir.exists():
        shutil.rmtree(clone_dir)
    run(["git", "clone", "--no-checkout", "--filter=blob:none", "--", info["url"], str(clone_dir)])
    rev = info.get("rev") or info.get("requested_rev") or info.get("tag") or info.get("branch")
    if not rev:
        raise RuntimeError(f"git source {info['url']} did not include a lockfile revision")
    checkout = run(["git", "checkout", "--detach", rev], cwd=clone_dir, check=False)
    if checkout.returncode != 0:
        run(["git", "fetch", "--depth=1", "origin", rev], cwd=clone_dir)
        run(["git", "checkout", "--detach", rev], cwd=clone_dir)
    copy_tree_contents(clone_dir, dest, ignore={".git"})
    shutil.rmtree(clone_dir)


def vendor_source(package: Package, dest: Path, workdir: Path, ref: str, role: str, lockfile: str) -> None:
    worktree = workdir / "worktrees" / role
    vendor_dir = workdir / "vendor" / role
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "worktree", "add", "--detach", str(worktree), ref])

    if vendor_dir.exists():
        shutil.rmtree(vendor_dir)
    vendor_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_parent = Path(lockfile).parent
    vendor_cwd = worktree if str(lock_parent) == "." else worktree / lock_parent
    run(["cargo", "vendor", "--locked", "--versioned-dirs", str(vendor_dir)], cwd=vendor_cwd)

    candidates = sorted(vendor_dir.glob(f"{package.name}-{package.version}*"))
    candidates = [candidate for candidate in candidates if candidate.is_dir()]
    if len(candidates) != 1:
        raise RuntimeError(
            f"cargo vendor could not uniquely identify {package.name} {package.version} "
            f"from {lockfile}; candidates: {[candidate.name for candidate in candidates]}"
        )
    copy_tree_contents(candidates[0], dest)


def create_source_artifact(source_dir: Path, artifacts_dir: Path, slug: str) -> Path:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifacts_dir / f"{slug}-new-source.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        tar.add(source_dir, arcname=slug, recursive=True)
    return artifact


def create_synthetic_repo(change: Change, old_src: Path, new_src: Path, repo: Path) -> bool:
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    git_init(repo)
    run(["git", "config", "user.email", "dep-reviews-action@example.invalid"], cwd=repo)
    run(["git", "config", "user.name", "dep-reviews-action"], cwd=repo)
    run(["git", "config", "commit.gpgsign", "false"], cwd=repo)

    if change.old:
        copy_tree_contents(old_src, repo, ignore={".git"})
        run(["git", "add", "-A", "-f"], cwd=repo)
        run(["git", "commit", "-q", "-m", f"Import {change.old.name} {change.old.version}"], cwd=repo)
    else:
        run(
            [
                "git",
                "commit",
                "--allow-empty",
                "-q",
                "-m",
                f"Empty baseline before adding {change.new.name} {change.new.version}",
            ],
            cwd=repo,
        )

    clear_dir_except_git(repo)
    copy_tree_contents(new_src, repo, ignore={".git"})
    run(["git", "add", "-A", "-f"], cwd=repo)
    if run(["git", "diff", "--cached", "--quiet"], cwd=repo, check=False).returncode == 0:
        return False
    run(["git", "commit", "-q", "-m", f"Import {change.new.name} {change.new.version}"], cwd=repo)
    return True


def git_init(repo: Path) -> None:
    result = run(["git", "init", "-q", "-b", "main"], cwd=repo, check=False)
    if result.returncode != 0:
        run(["git", "init", "-q"], cwd=repo)
        run(["git", "checkout", "-q", "-b", "main"], cwd=repo)


def clear_dir_except_git(path: Path) -> None:
    for child in path.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_tree_contents(src: Path, dest: Path, ignore: Optional[set[str]] = None) -> None:
    ignore = ignore or set()
    dest.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        if child.name in ignore:
            continue
        target = dest / child.name
        if child.is_dir() and not child.is_symlink():
            shutil.copytree(child, target, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        else:
            shutil.copy2(child, target, follow_symlinks=False)


def parse_diff_hunks(raw_diff: str) -> list[dict[str, Any]]:
    hunks: list[dict[str, Any]] = []
    current_path = ""
    current_index = 0
    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            current_path = path_from_diff_header(line)
            current_index = 0
        elif line.startswith("@@ "):
            current_index += 1
            hunks.append({"path": current_path, "hunk_index": current_index, "header": line})
    return hunks


def path_from_diff_header(line: str) -> str:
    parts = line.split(" b/", 1)
    if len(parts) == 2:
        return parts[1]
    pieces = line.split()
    if len(pieces) >= 4:
        return pieces[3].removeprefix("b/")
    return "unknown"


def packet_skeleton(change: Change, hunks: list[dict[str, Any]]) -> str:
    refs = "\n".join(f"@hunk {hunk['path']}#{hunk['hunk_index']}" for hunk in hunks)
    if not refs:
        refs = "No textual hunks were detected. Review binary, mode, or metadata changes in the Changes view."

    title = f"Cargo dependency audit: {change.new.name} {change_label(change)}"
    if change.change_kind == "added":
        walkthrough = (
            f"Review the newly added external dependency source for `{change.new.name}` "
            f"`{change.new.version}`. Keep every changed line covered exactly once by these hunk references while reorganizing this packet."
        )
    elif change.change_kind == "source-migration" and change.old:
        walkthrough = (
            f"Review the dependency source migration for `{change.new.name}` from "
            f"`{change.old.version}` ({change.old.source_kind}) to `{change.new.version}` "
            f"({change.new.source_kind}). Keep every changed line covered exactly once by these hunk references while reorganizing this packet."
        )
    elif change.old:
        walkthrough = (
            f"Review the dependency source changes from `{change.old.version}` to `{change.new.version}`. "
            "Keep every changed line covered exactly once by these hunk references while reorganizing this packet."
        )
    else:
        walkthrough = (
            f"Review the newly added external dependency source for `{change.new.name}` "
            f"`{change.new.version}`. Keep every changed line covered exactly once by these hunk references while reorganizing this packet."
        )

    return textwrap.dedent(
        f"""\
        # {title}

        Automated source diff for external Cargo dependency `{change.new.name}`.

        ## Security audit findings

        Replace this section with the audit verdict, notable security findings, and any suspicious build script, proc-macro, unsafe, network, filesystem, process, or credential-handling changes.

        ## Guided source walkthrough

        {walkthrough}

        {refs}
        """
    )


def local_checkout_command(package: Package, slug: str, tarball_name: str) -> str:
    if package.source_kind == "crates.io":
        checksum_line = ""
        if package.checksum:
            archive = f"{package.name}-{package.version}.crate"
            checksum_line = (
                "if command -v sha256sum >/dev/null 2>&1; then\n"
                f"  echo '{package.checksum}  {archive}' | sha256sum -c -\n"
                "else\n"
                f"  test \"$(shasum -a 256 {shell_quote(archive)} | awk '{{print $1}}')\" = {shell_quote(package.checksum)}\n"
                "fi\n"
            )
        return (
            f"mkdir -p {shell_quote(package.name + '-' + package.version)} && "
            f"cd {shell_quote(package.name + '-' + package.version)}\n"
            f"curl -L {shell_quote(crates_download_url(package.name, package.version))} "
            f"-o {shell_quote(package.name + '-' + package.version + '.crate')}\n"
            f"{checksum_line}"
            f"tar -xzf {shell_quote(package.name + '-' + package.version + '.crate')} --strip-components=1\n"
            "git init && git add -A -f && git commit -m 'Import dependency source'\n"
        )

    if package.source_kind == "git":
        info = parse_git_source(package.source or "")
        rev = info.get("rev") or info.get("requested_rev") or info.get("tag") or info.get("branch")
        return (
            f"git clone {shell_quote(info['url'])} {shell_quote(package.name + '-' + package.version)}\n"
            f"cd {shell_quote(package.name + '-' + package.version)} && git checkout --detach {shell_quote(rev)}\n"
        )

    run_id = os.environ.get("GITHUB_RUN_ID", "<run-id>")
    repo = os.environ.get("GITHUB_REPOSITORY", "Avi-D-coder/dep-reviews-packet-action")
    artifact = artifact_name()
    return (
        f"gh run download {shell_quote(run_id)} --repo {shell_quote(repo)} --name {shell_quote(artifact)}\n"
        f"mkdir -p {shell_quote(slug)} && tar -xzf {shell_quote(artifact + '/' + tarball_name)} "
        f"-C {shell_quote(slug)} --strip-components=1\n"
        f"cd {shell_quote(slug)} && git init && git add -A -f && git commit -m 'Import dependency source'\n"
    )


def artifact_name() -> str:
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    return f"dep-review-new-sources-{run_id}"


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def write_manifest(workdir: Path, manifest: dict[str, Any]) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    write_json(workdir / "manifest.json", manifest)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_outputs(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={value}\n")


def run(
    cmd: list[str],
    cwd: Optional[Path] = None,
    check: bool = True,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"`{' '.join(cmd)}` failed with exit {result.returncode}\n{result.stderr.strip()}"
        )
    return result


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
