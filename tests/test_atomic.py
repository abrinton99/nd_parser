"""Atomic-write tests (spec §18.4): seen.json is never observed half-written."""

from __future__ import annotations

import json
import os

import pytest

from nextdoor_watcher import util
from nextdoor_watcher.util import atomic_write_json, read_json


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "seen.json"
    data = {"c_aaa": {"status": "live"}}
    atomic_write_json(p, data)
    assert read_json(p) == data


def test_no_tmp_files_left_after_success(tmp_path):
    p = tmp_path / "seen.json"
    atomic_write_json(p, {"a": 1})
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []


def test_failure_midwrite_preserves_original_and_cleans_tmp(tmp_path, monkeypatch):
    p = tmp_path / "seen.json"
    atomic_write_json(p, {"version": 1})           # known-good original

    # Force a failure after the temp file is opened but before os.replace.
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(util.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(p, {"version": 2})
    monkeypatch.setattr(util.os, "replace", real_replace)

    # Original is intact (never half-written) and no temp file remains.
    assert read_json(p) == {"version": 1}
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []


def test_original_intact_when_serialization_fails(tmp_path):
    p = tmp_path / "seen.json"
    atomic_write_json(p, {"ok": True})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        atomic_write_json(p, {"bad": Unserializable()})
    # File still holds the previous good content.
    assert json.loads(p.read_text()) == {"ok": True}
