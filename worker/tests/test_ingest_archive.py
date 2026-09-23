"""Reading an uploaded zip: limits, and reading only what is needed, once.

Each member used to be capped at 200 MB and nothing else was: a zip of many members under the
cap went through, every member was decompressed into memory, and it all happened twice
because the board and its sidecar files were read in separate passes.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from emi_worker.stages import StageError
from emi_worker.stages import ingest

FIXTURES = Path(__file__).parent / "fixtures"
BOARD = (FIXTURES / "tiny.kicad_pcb").read_bytes()
PROJECT = b'{"net_settings": {"classes": [{"name": "Default"}], "meta": {"version": 3}}}'


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, blob in members.items():
            z.writestr(name, blob)
    return buf.getvalue()


@pytest.fixture
def reads(monkeypatch):
    """Every member name zipfile decompresses, in order."""
    seen: list[str] = []
    real = zipfile.ZipFile.open

    def spy(self, name, mode="r", *a, **kw):
        if mode == "r":  # not the writes that build the test zip
            seen.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
        return real(self, name, mode, *a, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    return seen


def test_only_the_needed_members_are_read_and_each_once(reads):
    data = _zip({
        "proj/tiny.kicad_pcb": BOARD,
        "proj/tiny.kicad_pro": PROJECT,
        "proj/emi.rules.yaml": b"rules: {}\n",
        "proj/3d/part.step": b"\0" * 2_000_000,
        "proj/tiny.kicad_sch": b"(kicad_sch)",
        "proj/fab/tiny-F_Silkscreen.gbr": b"%FSLAX46Y46*%",
    })
    model, filename, kind, sidecars = ingest.read_upload(data)

    assert (filename, kind) == ("proj/tiny.kicad_pcb", "kicad")
    assert model.pads and sidecars.project == PROJECT and sidecars.settings is not None
    assert sorted(reads) == ["proj/emi.rules.yaml", "proj/tiny.kicad_pcb", "proj/tiny.kicad_pro"]


def test_a_gerber_zip_reads_only_the_fabrication_files(reads):
    gerbers = {p.name: p.read_bytes() for p in (FIXTURES / "gerber").iterdir()}
    data = _zip({**{f"fab/{n}": b for n, b in gerbers.items()},
                 "fab/tiny-F_Paste.gtp": b"%FSLAX46Y46*%", "photo.jpg": b"\xff" * 1000})
    _, _, kind, _ = ingest.read_upload(data)
    assert kind == "gerber"
    assert sorted(reads) == sorted(f"fab/{n}" for n in gerbers)


def test_too_many_members_is_refused(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_ARCHIVE_MEMBERS", 20)
    data = _zip({"tiny.kicad_pcb": BOARD, **{f"junk/{i}.txt": b"x" for i in range(25)}})
    with pytest.raises(StageError, match="lists 26 files"):
        ingest.read_upload(data)


def test_the_total_read_is_capped_not_just_each_member(monkeypatch):
    """Many members, each under the per-member cap: a zip bomb in instalments."""
    monkeypatch.setattr(ingest, "MAX_ARCHIVE_READ_BYTES", 1_000_000)
    data = _zip({f"fab/layer{i}.gbr": b"\0" * 400_000 for i in range(5)})
    with pytest.raises(StageError, match="add up to more than"):
        ingest.read_upload(data)


def test_what_the_archive_claims_to_hold_is_capped(monkeypatch, reads):
    """Refused from the directory alone, before anything is decompressed."""
    monkeypatch.setattr(ingest, "MAX_ARCHIVE_TOTAL_BYTES", 1_000_000)
    data = _zip({"tiny.kicad_pcb": BOARD, "3d/part.step": b"\0" * 2_000_000})
    with pytest.raises(StageError, match="unpacks to"):
        ingest.read_upload(data)
    assert reads == []


def test_one_member_over_the_cap_is_refused(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_BOARD_BYTES", 10_000)
    data = _zip({"big.kicad_pcb": b"(kicad_pcb " + b" " * 20_000 + b")"})
    with pytest.raises(StageError, match="larger than"):
        ingest.load_board(data)


def test_load_board_and_load_sidecars_still_work_alone():
    """The cable and transient stages call them separately."""
    data = _zip({"tiny.kicad_pcb": BOARD, "tiny.kicad_pro": PROJECT})
    model, _, kind = ingest.load_board(data)
    assert kind == "kicad" and model.pads
    assert ingest.load_sidecars(data).project == PROJECT
    assert ingest.load_sidecars(BOARD).project is None


def test_a_dotfile_rules_document_is_found():
    """.emi.yaml is a supported name, but dotfiles were skipped before it was looked for."""
    data = _zip({"tiny.kicad_pcb": BOARD, ".emi.yaml": b"rules: {}\n"})
    _, _, _, sidecars = ingest.read_upload(data)
    assert sidecars.settings is not None
