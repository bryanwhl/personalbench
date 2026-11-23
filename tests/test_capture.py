import json

import pytest

from personalbench.capture import capture_finish, capture_start, capture_watch
from personalbench.imports import build_benchmark


def session_file(tmp_path):
    path = tmp_path / "synthetic-session.json"
    path.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "text": "Fix parser"},
                    {"role": "assistant", "text": "Done"},
                ]
            }
        )
    )
    return path


def test_snapshot_preserves_start_and_excludes_secrets(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "parser.py").write_text("raise ValueError('bug')\n")
    (source / ".env").write_text("SECRET=value")
    (source / ".git").mkdir()
    (source / ".git" / "history").write_text("future answer")
    output = tmp_path / "capture"
    manifest = capture_start(source, output)
    (source / "parser.py").write_text("print('fixed')\n")
    session = capture_finish(output, session_file(tmp_path))
    assert session["inputs"] == {"parser.py": "raise ValueError('bug')\n"}
    assert len(manifest["excluded"]) == 2
    assert (output.stat().st_mode & 0o777) == 0o700
    assert (output / "session.json").stat().st_mode & 0o777 == 0o600
    assert build_benchmark(session)["tasks"][0]["provenance"]["starting_state"] == "captured"


def test_capture_without_source_remains_missing(tmp_path):
    output = tmp_path / "capture"
    capture_start(None, output)
    session = capture_finish(output, session_file(tmp_path))
    assert "inputs" not in session
    assert build_benchmark(session)["tasks"][0]["provenance"]["starting_state"] == "missing"


def test_symlinks_and_contained_output_fail_closed(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "link").symlink_to(tmp_path / "other")
    with pytest.raises(ValueError, match="Nonregular"):
        capture_start(source, tmp_path / "capture")
    with pytest.raises(ValueError, match="outside"):
        capture_start(source, source / "capture")


def test_changed_snapshot_rejected(tmp_path):
    source = tmp_path / "file.txt"
    source.write_text("original")
    output = tmp_path / "capture"
    capture_start(source, output)
    (output / "start-state" / "file.txt").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        capture_finish(output, session_file(tmp_path))


def test_traversal_manifest_rejected(tmp_path):
    output = tmp_path / "capture"
    capture_start(None, output)
    manifest = json.loads((output / "capture.json").read_text())
    manifest["files"] = [{"path": "../outside", "sha256": "x"}]
    (output / "capture.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Unsafe"):
        capture_finish(output, session_file(tmp_path))


def test_bounded_watch_single_update_and_validation(tmp_path):
    output = tmp_path / "capture"
    capture_start(None, output)
    path = session_file(tmp_path)
    updates = list(capture_watch(output, path, duration_seconds=0))
    assert len(updates) == 1
    with pytest.raises(ValueError, match="interval"):
        list(capture_watch(output, path, interval_seconds=0))


def test_existing_capture_not_overwritten(tmp_path):
    output = tmp_path / "capture"
    capture_start(None, output)
    with pytest.raises(ValueError, match="empty"):
        capture_start(None, output)


def test_capture_export_reimport_preserves_start_state(tmp_path):
    from personalbench.imports import import_session

    source = tmp_path / "input.txt"
    source.write_text("starting data")
    output = tmp_path / "capture"
    capture_start(source, output)
    capture_finish(output, session_file(tmp_path))
    imported = import_session(output / "session.json")
    assert imported["inputs"] == {"input.txt": "starting data"}


def test_capture_secret_content_rejected_before_copy(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("-----BEGIN " + "PRIVATE KEY-----")
    output = tmp_path / "capture"
    with pytest.raises(ValueError, match="credential"):
        capture_start(source, output)
    assert not (output / "start-state").exists()
