"""Packaging for KiCad's Plugin and Content Manager."""

import json
import zipfile

import pytest

from scripts import pcm_release
from scripts.pcm_release import ReleaseError, release


@pytest.fixture
def repo(tmp_path):
    return tmp_path / "kicad-plugins"


def build(repo, version="0.1.0", **kw):
    return release(
        version,
        repo,
        download_url=f"https://example.com/emi-analyzer-kicad-plugin-{version}.zip",
        repo_url="https://example.com/repo",
        archive_dir=repo.parent / "dist",
        now=1_700_000_000,
        **kw,
    )


def test_the_archive_is_laid_out_the_way_pcm_installs_it(repo):
    out = build(repo)
    with zipfile.ZipFile(out["archive"]) as z:
        names = set(z.namelist())
        metadata = json.loads(z.read("metadata.json"))
    assert "plugins/plugin.json" in names
    assert "plugins/analyze.py" in names
    assert "plugins/emi_analyzer/app.py" in names
    assert "plugins/icons/emi-24.png" in names
    assert "plugins/LICENSE" in names  # taken from the repository, not copied into the plugin
    assert "resources/icon.png" in names
    assert not any(n.startswith("plugins/tests/") or "__pycache__" in n for n in names)
    assert [v["version"] for v in metadata["versions"]] == ["0.1.0"]


def test_the_same_inputs_give_the_same_archive(repo, tmp_path):
    a = build(repo)["archive"].read_bytes()
    b = build(tmp_path / "again")["archive"].read_bytes()
    assert a == b


def test_a_version_the_package_does_not_declare_is_refused(repo):
    with pytest.raises(ReleaseError) as e:
        build(repo, version="99.0.0")
    assert "__version__" in str(e.value)


def test_the_repository_keeps_every_other_package(repo):
    repo.mkdir(parents=True)
    (repo / "packages.json").write_text(
        json.dumps(
            {"packages": [{"identifier": "com.embeddedci.pcb-trace-length-analyzer", "versions": [{"version": "0.1.2"}]}]}
        )
    )
    build(repo)
    packages = json.loads((repo / "packages.json").read_text())["packages"]
    assert [p["identifier"] for p in packages] == [
        "com.embeddedci.emi-analyzer",
        "com.embeddedci.pcb-trace-length-analyzer",
    ]


def test_an_earlier_version_of_this_plugin_stays_available(repo, monkeypatch):
    repo.mkdir(parents=True)
    (repo / "packages.json").write_text(
        json.dumps({"packages": [{"identifier": "com.embeddedci.emi-analyzer", "versions": [{"version": "0.0.9"}]}]})
    )
    build(repo)
    packages = json.loads((repo / "packages.json").read_text())["packages"]
    assert [v["version"] for v in packages[0]["versions"]] == ["0.1.0", "0.0.9"]


def test_the_repository_index_hashes_what_it_points_at(repo):
    build(repo)
    index = json.loads((repo / "repository.json").read_text())
    assert index["packages"]["url"].endswith("/packages.json")
    assert index["packages"]["sha256"] == pcm_release.sha256(repo / "packages.json")
    assert index["resources"]["sha256"] == pcm_release.sha256(repo / "resources.zip")
    assert index["packages"]["update_time_utc"] == "2023-11-14 22:13:20"
