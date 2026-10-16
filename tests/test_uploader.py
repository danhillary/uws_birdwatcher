"""Tests for the sync thread's per-detection step (no network or database)."""
import datetime
import os

import pytest

import config
import uploader

NOW = datetime.datetime(2026, 10, 16, 12, 0, 0)


@pytest.fixture
def clips(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLIPS_DIR", str(tmp_path))
    return tmp_path


def _row(clip, upload=True, age_s=60):
    return dict(detected_at=NOW - datetime.timedelta(seconds=age_s),
                common_name="Blue Jay", scientific_name="Cyanocitta cristata",
                confidence=0.9, lat=40.776, lon=-73.977,
                clip_path=clip, clip_url=None, upload_clip=upload)


def _clip(clips, name="20261016T115900_Blue_Jay.wav"):
    (clips / name).write_bytes(b"RIFF....WAVE")
    return name


def _ok_put(name, data):
    return f"https://bucket.example/{name}"


def _bad_put(name, data):
    raise ConnectionError("S3 unreachable")


def test_uploaded_clip_is_written_as_url_then_deleted_locally(clips):
    name = _clip(clips)
    inserted = []
    uploader.sync_row(_row(name), insert=lambda **r: inserted.append(r),
                      put=_ok_put, uploads_on=True, now=NOW)
    assert inserted[0]["clip_url"] == f"https://bucket.example/{name}"
    assert inserted[0]["clip_path"] is None
    assert "upload_clip" not in inserted[0]
    assert not os.path.exists(clips / name)


def test_failed_db_insert_keeps_the_local_clip(clips):
    name = _clip(clips)

    def insert(**r):
        raise ConnectionError("db down")

    with pytest.raises(ConnectionError):
        uploader.sync_row(_row(name), insert=insert, put=_ok_put,
                          uploads_on=True, now=NOW)
    assert os.path.exists(clips / name)  # retried (and re-uploaded) next pass


def test_recent_upload_failure_keeps_the_row_queued(clips):
    name = _clip(clips)
    inserted = []
    with pytest.raises(ConnectionError):
        uploader.sync_row(_row(name), insert=lambda **r: inserted.append(r),
                          put=_bad_put, uploads_on=True, now=NOW)
    assert inserted == []
    assert os.path.exists(clips / name)


def test_long_failing_upload_writes_the_row_with_the_local_clip(clips):
    name = _clip(clips)
    inserted = []
    uploader.sync_row(_row(name, age_s=config.CLIP_UPLOAD_GIVE_UP_SECONDS + 1),
                      insert=lambda **r: inserted.append(r),
                      put=_bad_put, uploads_on=True, now=NOW)
    assert inserted[0]["clip_path"] == name and inserted[0]["clip_url"] is None
    assert os.path.exists(clips / name)  # left for backfill_clips


def test_human_voice_clip_never_uploads(clips):
    name = _clip(clips)
    inserted = []

    def put(name, data):
        raise AssertionError("a human-voice clip must never be uploaded")

    uploader.sync_row(_row(name, upload=False), insert=lambda **r: inserted.append(r),
                      put=put, uploads_on=True, now=NOW)
    assert inserted[0]["clip_path"] == name
    assert os.path.exists(clips / name)


def test_pruned_clip_still_saves_the_detection(clips):
    inserted = []
    uploader.sync_row(_row("gone.wav"), insert=lambda **r: inserted.append(r),
                      put=_ok_put, uploads_on=True, now=NOW)
    assert inserted[0]["clip_path"] is None and inserted[0]["clip_url"] is None


def test_uploads_off_keeps_clip_local(clips):
    name = _clip(clips)
    inserted = []
    uploader.sync_row(_row(name), insert=lambda **r: inserted.append(r),
                      put=_bad_put, uploads_on=False, now=NOW)
    assert inserted[0]["clip_path"] == name
    assert os.path.exists(clips / name)
