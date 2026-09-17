"""Deterministic approval policy over recorded shell actions.

The policy decides which recorded commands need an explicit user approval before the
sandbox runs them. It understands the common shell write forms listed below and nothing
else: this is a user-visible control point, not the security boundary. The enforced
boundary remains the sandbox, the allowed-path check on the candidate tree and the
independent acceptance run.

Quoted text and heredoc bodies are treated as data, not shell syntax, so a comparison or
a file name inside an embedded script is not read as a redirection. The remaining limits
are reported rather than hidden: write forms such as ``python -c "open(...,'w')"`` cannot
be resolved to paths, and a quoted redirection target is not resolved either.
"""

import re
from pathlib import PurePosixPath

WORKSPACE = "/workspace"

# Target of a redirection: `cat > src/order.py`, `echo x >> log.txt`, `echo x>log.txt`.
# A preceding word character is allowed so `cmd>file` is seen; fd redirects (`2>&1`) are not.
REDIRECT = re.compile(r"(?:^|[\s;|&()\w])(?:[0-9])?(>>?)(?![&>])\s*([^\s;&|<>()]+)")
# Shell separators; quoted separators are blanked out before splitting.
SEPARATOR = re.compile(r"&&|\|\||[;&|\n]")
# A heredoc opener; group 1 is the delimiter that ends the body.
HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
# Python-level writes cannot be resolved to paths, so they only widen the strict policy.
PYTHON_WRITE = re.compile(r"write_text\s*\(|write_bytes\s*\(|\.write\s*\(|open\s*\([^)]*,\s*['\"][wa]")
WRITE_TOOLS = {"tee", "cp", "mv", "rm", "unlink", "sed", "truncate", "install", "touch", "dd", "mkdir", "rmdir", "ln"}


def _shell_view(command):
    """Command text with quoted regions blanked and heredoc bodies dropped.

    A heredoc body is data, but the commands that follow a finished heredoc are still
    shell syntax: only the body is dropped, not everything after the opener, or a write
    placed after the terminator would never be seen. An unterminated heredoc leaves the
    rest of the command inside its body, which is the conservative reading.
    """
    view, quote, pending = [], None, None
    for line in command.split("\n"):
        if pending is not None:
            pending = None if line.strip() == pending else pending
            continue
        blanked, opener = [], None
        for index, char in enumerate(line):
            if quote:
                quote = None if char == quote else quote
                blanked.append(" ")
            elif char in "'\"":
                quote = char
                blanked.append(" ")
            elif opener is None and line.startswith("<<", index):
                # Read the delimiter from the original text: it is usually quoted, and the
                # quoted characters are blanked in the view.
                match = HEREDOC.match(line, index)
                opener = match.group(1) if match else None
                blanked.append(char)
            else:
                blanked.append(char)
        view.append("".join(blanked))
        pending = opener
    return "\n".join(view)


def _bare(value):
    return value.strip().strip("'\"")


def _resolve(raw, cwd=WORKSPACE):
    """Map a target to a workspace path; absolute paths outside the workspace stay absolute.

    Commands are POSIX shell text, so paths are handled as POSIX paths regardless of the
    host the policy happens to run on.
    """
    value = _bare(raw)
    path = PurePosixPath(value)
    if not path.is_absolute():
        path = PurePosixPath(cwd) / path
    if str(path) == "/tmp" or str(path).startswith(("/tmp/", "/dev/")):
        return None  # ephemeral inside the sandbox, discarded with the container
    try:
        relative = str(path.relative_to(WORKSPACE))
    except ValueError:
        return str(path)
    return "/" if relative == "." else "/" + relative


def _tool_targets(name, segment):
    args = segment.split()[1:]
    if name == "sed":
        if not any(word == "-i" or word.startswith("-i.") or word.startswith("--in-place") for word in args):
            return []  # only in-place edits write
        values = [word for word in args if not word.startswith("-")]
        return values[1:] if len(values) > 1 else []
    if name == "dd":
        return [word[3:] for word in args if word.startswith("of=")]
    values = [word for word in args if not word.startswith("-")]
    if name in {"cp", "mv", "install", "ln"}:
        return values[-1:] if values else []
    return values


def _write_targets(command):
    """Workspace targets this command plausibly writes, in recording order."""
    view = _shell_view(command)
    targets = []
    for match in REDIRECT.finditer(view):
        target = _resolve(match.group(2))
        if target:
            targets.append(target)
    for segment in [part for part in SEPARATOR.split(view) if part.strip()]:
        words = segment.split()
        if not words:
            continue
        name = PurePosixPath(words[0]).name
        if name not in WRITE_TOOLS:
            continue
        for raw in _tool_targets(name, segment):
            target = _resolve(raw)
            if target:
                targets.append(target)
    return targets


def _allowed(path, allowed_paths):
    return any(path == f"/{entry}" or path.startswith("/" + entry.rstrip("/") + "/") for entry in allowed_paths)


def evaluate(command, allowed_paths, policy="auto"):
    """Decide whether an action needs approval; never raises on unusual input."""
    if not isinstance(command, str) or policy == "off":
        return {"requires_approval": False, "reason": "", "policy": policy, "targets": []}
    targets = [{"path": path, "allowed": _allowed(path, allowed_paths)}
               for path in dict.fromkeys(_write_targets(command))]
    outside = [t["path"] for t in targets if not t["allowed"]]
    if policy == "strict":
        if targets:
            return {"requires_approval": True, "policy": policy, "targets": targets,
                    "reason": "已启用严格审批：该动作会写入工作区文件（" + ", ".join(t["path"] for t in targets) + "）。"}
        if PYTHON_WRITE.search(command):
            return {"requires_approval": True, "policy": policy, "targets": targets,
                    "reason": "已启用严格审批：该动作包含 Python 写文件调用，无法从命令中解析具体路径。"}
    if policy == "auto" and outside:
        return {"requires_approval": True, "policy": policy, "targets": targets,
                "reason": "该动作写入允许范围之外的位置（" + ", ".join(outside) + "）。"}
    return {"requires_approval": False, "policy": policy, "targets": targets, "reason": ""}
