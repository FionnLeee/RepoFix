from repopilot.policy import evaluate


def test_allowed_and_read_only_actions_do_not_require_approval():
    assert evaluate("cat > pricing.py <<'PY'\nx=1\nPY", ["pricing.py"])["requires_approval"] is False
    assert evaluate("python -m unittest -v", ["pricing.py"])["requires_approval"] is False
    assert evaluate("cat pricing.py", ["pricing.py"])["requires_approval"] is False
    assert evaluate("rm pricing.py", ["pricing.py"])["requires_approval"] is False
    assert evaluate("sed -i s/a/b/ src/order.py", ["src/order.py"])["requires_approval"] is False
    assert evaluate("find . -type f", ["src/order.py"])["requires_approval"] is False


def test_writes_outside_allowed_paths_require_approval_in_auto_policy():
    verdict = evaluate("echo hacked > conftest.py", ["pricing.py"])
    assert verdict["requires_approval"] is True
    assert verdict["targets"] == [{"path": "/conftest.py", "allowed": False}]
    assert "conftest.py" in verdict["reason"]
    assert evaluate("echo ignore >src/other.py", ["src/order.py"])["requires_approval"] is True
    assert evaluate("mv src/order.py src/moved.py", ["src/order.py"])["requires_approval"] is True
    assert evaluate("sed -i s/a/b/ tests/helper.py", ["src/order.py"])["requires_approval"] is True
    assert evaluate("cat > /etc/passwd", ["pricing.py"])["requires_approval"] is True


def test_strict_policy_requires_approval_for_any_write():
    verdict = evaluate("echo x > pricing.py", ["pricing.py"], "strict")
    assert verdict["requires_approval"] is True and verdict["policy"] == "strict"
    assert evaluate("cat pricing.py", ["pricing.py"], "strict")["requires_approval"] is False
    python_write = evaluate("python -c \"from pathlib import Path; Path('pricing.py').write_text('x')\"",
                            ["pricing.py"], "strict")
    assert python_write["requires_approval"] is True and python_write["targets"] == []
    assert "Python" in python_write["reason"]
    # The same command stays invisible to the auto policy: its targets cannot be resolved.
    assert evaluate("python -c \"from pathlib import Path; Path('other.py').write_text('x')\"",
                    ["pricing.py"])["requires_approval"] is False


def test_ephemeral_targets_and_disabled_policy():
    assert evaluate("echo x > /tmp/scratch", ["pricing.py"])["requires_approval"] is False
    assert evaluate("echo x > /dev/null", ["pricing.py"])["requires_approval"] is False
    assert evaluate("echo x > /tmp/scratch", ["pricing.py"], "strict")["requires_approval"] is False
    assert evaluate("echo x > other.py", ["pricing.py"], "off")["requires_approval"] is False
    assert evaluate(None, ["pricing.py"])["requires_approval"] is False


def test_quoted_and_heredoc_text_is_data_not_shell_syntax():
    # A comparison inside an embedded script is not a redirection.
    script = "python -c 'import json; from pathlib import Path; d=json.loads(\"{}\"); [Path(k).write_text(v) for k, v in d.items() if 1 > 0]'"
    assert evaluate(script, ["src/order.py"])["targets"] == []
    assert evaluate('echo "a > b"', ["pricing.py"])["requires_approval"] is False
    heredoc = "cat > pricing.py <<'PY'\nif amount > 0:\n    pass\nPY"
    assert evaluate(heredoc, ["pricing.py"])["targets"] == [{"path": "/pricing.py", "allowed": True}]
    assert evaluate("python -c 'x = 1' && cat > src/order.py <<EOF\n1 > 2\nEOF",
                    ["src/order.py"])["targets"] == [{"path": "/src/order.py", "allowed": True}]


def test_only_a_heredoc_body_is_data_not_the_commands_after_it():
    # A write placed after the terminator is still shell syntax; dropping everything from
    # the opener would have hidden it from the approval gate.
    verdict = evaluate("cat > src/order.py <<'PY'\nif amount > 0:\n    pass\nPY\necho x > conftest.py",
                       ["src/order.py"])
    assert verdict["requires_approval"] is True
    assert [target["path"] for target in verdict["targets"]] == ["/src/order.py", "/conftest.py"]
    # A redirect that follows the opener on the same line is a write as well.
    assert evaluate("cat <<'PY' > src/order.py\ntext\nPY", ["src/order.py"])["targets"] == [
        {"path": "/src/order.py", "allowed": True}]
    # Two heredocs in one command: each opener keeps its own body.
    assert evaluate("cat > a.py <<'E1'\nx > 1\nE1\ncat > b.py <<'E2'\ny > 2\nE2", ["a.py", "b.py"])["targets"] == [
        {"path": "/a.py", "allowed": True}, {"path": "/b.py", "allowed": True}]
    # An unterminated heredoc leaves the rest of the command inside its body.
    assert evaluate("cat > a.py <<'EOF'\nstill > other.py", ["a.py"])["targets"] == [{"path": "/a.py", "allowed": True}]


def test_unrecognised_write_forms_are_a_known_limit():
    # Python-level writes are deliberately not modelled: the sandbox and the allowed-path
    # check remain the enforced boundary, and this is reported as a limit, not hidden.
    assert evaluate("python -c \"open('x.py','w').write('1')\"", ["pricing.py"])["targets"] == []
