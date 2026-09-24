from repofix.reaper import reap


class FakeContainer:
    def __init__(self, id, labels, fail=False):
        self.id, self.labels, self.fail = id, labels, fail
        self.removals = []

    def remove(self, force=False):
        if self.fail:
            raise RuntimeError("container is busy")
        self.removals.append({"force": force})


class FakeDocker:
    def __init__(self, containers):
        self._containers, self.containers = containers, self

    def list(self, all=False, filters=None):
        assert all is True and filters == {"label": "repofix.managed=sandbox"}
        return self._containers


def container(id, run, age, now=1000.0, fail=False):
    return FakeContainer(id, {"repofix.managed": "sandbox", "repofix.run": run,
                              "repofix.created": str(now - age)}, fail)


def test_reaper_removes_only_old_orphaned_sandboxes():
    containers = [container("fresh", "gone", age=10), container("orphan", "gone", age=500),
                  container("owned", "live", age=500)]
    result = reap(FakeDocker(containers), {"live"}, min_age=120, now=1000.0)
    assert [item["container"] for item in result["removed"]] == ["orphan"]
    assert containers[1].removals == [{"force": True}]
    assert containers[0].removals == [] and containers[2].removals == []
    assert {item["container"] for item in result["kept"]} == {"fresh", "owned"}


def test_reaper_keeps_unlabelled_age_and_reports_failures():
    unlabelled = FakeContainer("unlabelled", {"repofix.managed": "sandbox"})
    failing = container("failing", "gone", age=500, fail=True)
    result = reap(FakeDocker([unlabelled, failing]), set(), min_age=120, now=1000.0)
    assert [item["container"] for item in result["failed"]] == ["failing"]
    assert result["failed"][0]["error"] == "container is busy"
    assert unlabelled.removals == [{"force": True}]
