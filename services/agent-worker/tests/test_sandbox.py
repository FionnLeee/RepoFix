import threading
import uuid

import docker
import pytest
from repofix.fixture import FIXED, SOURCE
from repofix.runtime import Sandbox


@pytest.mark.docker
def test_sandbox_permissions_candidate_validation_and_cleanup():
    sandbox = Sandbox(f"test-{uuid.uuid4()}", lambda *_: None, threading.Event())
    container_id = sandbox.container.id
    try:
        sandbox.container.reload()
        config = sandbox.container.attrs
        assert config["HostConfig"]["NetworkMode"] == "none"
        assert config["HostConfig"]["ReadonlyRootfs"] is True
        assert config["Config"]["User"] == "1000:1000"
        assert not any("API_KEY" in value or "WORKER_TOKEN" in value for value in config["Config"]["Env"])
        assert sandbox.read_source() == SOURCE
        assert sandbox.execute({"command": "touch /forbidden"})["returncode"] != 0
        assert sandbox.execute({"command": "test ! -e /var/run/docker.sock"})["returncode"] == 0
        sandbox.put({"pricing.py": FIXED})
        assert sandbox.read_source() == FIXED
        sandbox.execute({"command": "rm pricing.py; ln -s /etc/passwd pricing.py"})
        with pytest.raises(ValueError):
            sandbox.read_source()
        with pytest.raises(ValueError):
            sandbox.put({"../unexpected.py": "bad"})
    finally:
        sandbox.close()
    check = docker.from_env()
    try:
        assert not check.containers.list(all=True, filters={"id": container_id})
    finally:
        check.close()
