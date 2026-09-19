"""Bounded, immutable text snapshots. Repository code is never executed here."""

import hashlib
import io
import json
import os
import re
import tarfile
from pathlib import Path, PurePosixPath
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_BYTES = 4 * 1024 * 1024
MAX_FILES = 200


def safe_path(value: str) -> str:
    if (
        not value or len(value) > 240 or "\\" in value or value.startswith("/")
        or any(p in ("", ".", "..") or p.lower() == ".git" for p in value.split("/"))
        or not re.fullmatch(r"[a-zA-Z0-9_./-]+", value)
    ):
        raise ValueError("Expected a relative repository path without traversal or .git")
    return value


def digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def validate_files(files: dict[str, str]) -> dict[str, str]:
    if not files or len(files) > MAX_FILES:
        raise ValueError("Snapshot must contain 1..200 text files")
    size = 0
    for path, content in files.items():
        safe_path(path)
        if not isinstance(content, str) or "\x00" in content or len(content.encode()) > 100000:
            raise ValueError("Only UTF-8 text files up to 100 KB are supported")
        size += len(content.encode())
    if size > MAX_BYTES:
        raise ValueError("Snapshot exceeds 4 MB")
    return files


class RepositoryTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(max_length=200)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    subdir: str = Field(default="", max_length=200)
    allowedPaths: list[str] = Field(max_length=30)
    instanceId: str | None = Field(default=None, max_length=120)
    verificationMode: Literal["tests", "harness"] = "tests"
    verificationFiles: dict[str, str] = Field(default_factory=dict)
    testCommand: str = Field(default="python -m unittest discover -v", min_length=1, max_length=1000)
    # A snapshot run copies a bounded text tree into a bare sandbox. An image run works inside
    # the repository that a prepared image already carries (SWE-bench instances ship their own
    # environment that way), so there is no snapshot to bound and no read-only root.
    workspaceMode: Literal["snapshot", "image"] = "snapshot"
    sandboxImage: str | None = Field(default=None, max_length=200)
    workspacePath: str = Field(default="/workspace", max_length=200)

    @model_validator(mode="after")
    def verification_shape(self):
        """Either this worker proves the patch, or it hands the proof to an external harness."""
        if self.verificationMode == "harness" and self.verificationFiles:
            raise ValueError("A harness-verified task must not carry acceptance tests")
        if self.verificationMode == "tests" and not self.verificationFiles:
            raise ValueError("Acceptance tests are required unless an external harness verifies the patch")
        return self

    @model_validator(mode="after")
    def workspace_shape(self):
        """An image workspace is the image's own repository: no snapshot to bound or restore."""
        if self.workspaceMode == "snapshot" and not self.allowedPaths:
            raise ValueError("A snapshot workspace requires explicit allowed paths")
        if self.workspaceMode == "image":
            if not self.sandboxImage:
                raise ValueError("An image workspace needs the image that carries the repository")
            if self.subdir:
                raise ValueError("An image workspace uses the whole repository, not a subtree")
            if self.verificationMode != "harness":
                raise ValueError("An image workspace cannot run local acceptance: no bounded snapshot")
        if self.workspaceMode == "snapshot" and self.sandboxImage:
            raise ValueError("A snapshot workspace uses the configured sandbox image")
        return self

    @field_validator("sandboxImage")
    @classmethod
    def image_shape(cls, value):
        if value is None:
            return value
        if not re.fullmatch(r"[a-zA-Z0-9][\w./-]*(?::[\w.-]+)?(?:@sha256:[0-9a-f]{64})?", value):
            raise ValueError("Use a plain image reference such as swebench/sweb.eval.x86_64.repo_1776_id:latest")
        return value

    @field_validator("workspacePath")
    @classmethod
    def workspace_shape_path(cls, value):
        if not re.fullmatch(r"/[\w./-]*", value) or ".." in value.split("/"):
            raise ValueError("Workspace path must be absolute and free of traversal")
        return value.rstrip("/") or "/"

    @field_validator("source")
    @classmethod
    def source_shape(cls, value):
        if not re.fullmatch(r"registered:[a-zA-Z0-9_-]{1,60}|https://github\.com/[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+", value):
            raise ValueError("Use registered:<id> or a public https://github.com/owner/repo URL")
        return value

    @field_validator("subdir")
    @classmethod
    def subdir_shape(cls, value):
        return safe_path(value) if value else value

    @field_validator("allowedPaths")
    @classmethod
    def paths_shape(cls, values):
        for value in values:
            safe_path(value)
            if value.startswith("_repopilot_verify/"):
                raise ValueError("Verification paths cannot be candidate paths")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate candidate path")
        return values

    @field_validator("verificationFiles")
    @classmethod
    def tests_shape(cls, value):
        if not value:
            return value  # a harness-verified task carries none; the model validator decides
        validate_files(value)
        if len(value) > 20 or not any(re.fullmatch(r"test_[a-zA-Z0-9_]+\.py", name) for name in value):
            raise ValueError("Provide top-level test_*.py unittest verification files")
        return value


def load_snapshot(spec: RepositoryTask) -> dict[str, str]:
    if spec.source.startswith("registered:"):
        repo_id = spec.source.split(":", 1)[1]
        path = Path(os.getenv("REPOSITORY_ROOT", "runtime/repositories")) / repo_id / f"{spec.commit}.json"
        if path.stat().st_size > MAX_BYTES * 3:
            raise ValueError("Registered snapshot is too large")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if snapshot["commit"] != spec.commit or snapshot["digest"] != digest(snapshot["files"]):
            raise ValueError("Registered snapshot provenance mismatch")
        files = validate_files(snapshot["files"])
    else:
        owner, repo = spec.source.removesuffix(".git").split("/")[-2:]
        url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/{spec.commit}"
        archive = bytearray()
        with httpx.stream("GET", url, timeout=30, follow_redirects=False) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                archive.extend(chunk)
                if len(archive) > 8 * 1024 * 1024:
                    raise ValueError("Repository archive exceeds 8 MB")
        files = {}
        prefix = spec.subdir + "/" if spec.subdir else ""
        expanded = 0
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for index, member in enumerate(tar):
                expanded += max(member.size, 0)
                if index > 10000 or expanded > 32 * 1024 * 1024:
                    raise ValueError("Repository archive expanded size limit")
                relative = str(PurePosixPath(member.name).relative_to(PurePosixPath(member.name).parts[0]))
                if not relative.startswith(prefix) or member.isdir():
                    continue
                if not member.isfile() or member.size > 100000:
                    raise ValueError("Selected subtree contains a link, special or oversized file")
                safe_path(relative)
                files[relative] = tar.extractfile(member).read().decode("utf-8")
                if len(files) > MAX_FILES:
                    raise ValueError("Too many files in selected subtree")
    prefix = spec.subdir + "/" if spec.subdir else ""
    selected = {name[len(prefix):]: value for name, value in files.items() if name.startswith(prefix)}
    if any(name.startswith("_repopilot_verify/") for name in selected):
        raise ValueError("Repository uses a reserved verification directory")
    return validate_files(selected)
