"""Packing the board for the app, and deciding which project it belongs to."""

import io
import json
import zipfile
from pathlib import Path

from emi_analyzer import project as projectlib
from emi_analyzer.boardio import BoardFiles
from emi_analyzer.project import Opened, ProjectStore, archive, open_board


def files(text=b"(kicad_pcb (version 20240108))", sidecars=None, name="demo.kicad_pcb", project_dir=None):
    return BoardFiles(
        text=text,
        filename=name,
        sidecars=dict(sidecars or {}),
        project_dir=project_dir,
        source="the editor",
    )


class FakeClient:
    """The app, as far as the upload flow can tell."""

    def __init__(self, lookup=None, projects=None):
        self._lookup = lookup or {"found": False}
        self.projects = dict(projects or {})
        self.created = []
        self.uploaded = []
        self.runs = {}
        self.artifacts = {}

    def lookup_board(self, sha256):
        return self._lookup

    def get_project(self, project_id):
        if project_id not in self.projects:
            raise RuntimeError("no such project")
        return {"id": project_id, "name": self.projects[project_id]}

    def create_project(self, name):
        pid = f"p{len(self.projects) + 1}"
        self.projects[pid] = name
        self.created.append(name)
        return {"id": pid, "name": name}

    def upload_board(self, project_id, filename, data, sha256):
        self.uploaded.append((project_id, filename, sha256, len(data)))
        return {"board": {"id": "b1"}, "run": {"id": "r1"}}

    def list_runs(self, project_id):
        return self.runs.get(project_id, [])

    def artifact(self, run_id, name):
        return self.artifacts[(run_id, name)]


# ---- the archive ----


def test_the_board_and_its_project_file_travel_together():
    data = archive(files(sidecars={"demo.kicad_pro": b"{}", "emi.rules.yaml": b"rules: []"}))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert sorted(z.namelist()) == ["demo.kicad_pcb", "demo.kicad_pro", "emi.rules.yaml"]
        assert z.read("demo.kicad_pcb").startswith(b"(kicad_pcb")


def test_the_same_board_packs_to_the_same_bytes():
    """Uploads are content-addressed: a timestamp in the zip would re-analyse every press."""
    assert archive(files()) == archive(files())
    assert archive(files()) != archive(files(text=b"(kicad_pcb (version 20240109))"))


# ---- which project ----


def test_a_board_the_app_already_analysed_is_only_opened():
    client = FakeClient(lookup={"found": True, "parsed": True, "project": {"id": "p7"}, "board": {"id": "b7"}})
    opened = open_board(client, None, files())
    assert opened == Opened(project_id="p7", run_id=None, board_id="b7", uploaded=False)
    assert client.uploaded == []


def test_a_board_whose_ingest_never_finished_is_sent_again():
    client = FakeClient(lookup={"found": True, "parsed": False, "project": {"id": "p7"}})
    opened = open_board(client, None, files())
    assert opened.uploaded and opened.run_id == "r1"
    assert client.created == ["demo"]


def test_an_edited_board_goes_into_the_project_its_file_already_has(tmp_path):
    """One board file is one project, so a design's history stays in one place."""
    store = ProjectStore(tmp_path)
    store.save("/work/demo.kicad_pcb", "p3")
    client = FakeClient(projects={"p3": "demo"})
    opened = open_board(client, store, files(project_dir=Path("/work")))

    assert opened.project_id == "p3"
    assert client.created == []
    assert client.uploaded[0][0] == "p3"


def test_a_project_deleted_in_the_app_is_replaced_rather_than_failing(tmp_path):
    store = ProjectStore(tmp_path)
    store.save("demo.kicad_pcb", "gone")
    client = FakeClient()

    opened = open_board(client, store, files())

    assert opened.project_id == "p1"
    assert store.load("demo.kicad_pcb") == "p1"


def test_a_forgotten_project_falls_back_to_what_the_app_already_has(tmp_path):
    """The remembered project is gone, but the board is there: show it, do not analyse it again."""
    store = ProjectStore(tmp_path)
    store.save("demo.kicad_pcb", "gone")
    client = FakeClient(lookup={"found": True, "parsed": True, "project": {"id": "p7"}, "board": {"id": "b7"}})

    opened = open_board(client, store, files())

    assert opened == Opened(project_id="p7", run_id=None, board_id="b7", uploaded=False)
    assert client.created == [] and client.uploaded == []
    assert store.load("demo.kicad_pcb") == "p7"


def test_a_lookup_that_fails_costs_an_upload_not_the_run():
    class Broken(FakeClient):
        def lookup_board(self, sha256):
            raise RuntimeError("the app is busy")

    opened = open_board(Broken(), None, files())
    assert opened.uploaded and opened.run_id == "r1"


# ---- what the checks found ----


def test_attention_is_every_flagged_net_worst_first_without_repeats():
    client = FakeClient()
    client.runs["p1"] = [
        {"id": "old", "kind": "ingest", "status": "done", "created_at": "2026-09-01T00:00:00Z"},
        {"id": "new", "kind": "ingest", "status": "done", "created_at": "2026-09-02T00:00:00Z"},
        {"id": "running", "kind": "ingest", "status": "in_progress", "created_at": "2026-09-03T00:00:00Z"},
        {"id": "solve", "kind": "solve", "status": "done", "created_at": "2026-09-04T00:00:00Z"},
    ]
    client.artifacts[("new", "rules.json")] = {
        "findings": [
            {"severity": "warning", "net": "USB_DP"},
            {"severity": "critical", "net": "DDR_A0"},
            {"severity": "critical", "net": "DDR_A0"},
            {"severity": "info", "net": "GND"},
            {"severity": "critical"},
        ]
    }

    assert projectlib.attention_nets(client, "p1") == ["DDR_A0", "USB_DP"]


def test_a_board_with_no_finished_analysis_has_nothing_to_select():
    client = FakeClient()
    client.runs["p1"] = [{"id": "r", "kind": "ingest", "status": "in_progress", "created_at": "2026-09-01"}]
    assert projectlib.attention_nets(client, "p1") == []


# ---- the store ----


def test_the_project_is_remembered_per_board_file(tmp_path):
    store = ProjectStore(tmp_path)
    store.save("/a/one.kicad_pcb", "p1")
    store.save("/b/two.kicad_pcb", "p2")
    assert store.load("/a/one.kicad_pcb") == "p1"
    assert store.load("/b/two.kicad_pcb") == "p2"
    assert store.load("/c/three.kicad_pcb") is None
    store.forget("/a/one.kicad_pcb")
    assert store.load("/a/one.kicad_pcb") is None


def test_a_settings_file_for_another_board_is_not_used(tmp_path):
    store = ProjectStore(tmp_path)
    store.save("/a/one.kicad_pcb", "p1")
    path = next((tmp_path / "boards").glob("*.json"))
    path.write_text(json.dumps({"board": "/somewhere/else.kicad_pcb", "project": "p9"}))
    assert store.load("/a/one.kicad_pcb") is None


def test_a_dev_copy_shares_the_released_plugins_settings():
    assert projectlib.shared_identifier("com.embeddedci.emi-analyzer.dev") == "com.embeddedci.emi-analyzer"
    assert projectlib.shared_identifier("com.embeddedci.emi-analyzer") == "com.embeddedci.emi-analyzer"
