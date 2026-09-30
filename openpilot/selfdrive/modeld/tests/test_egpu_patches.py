"""Guards for the fork's eGPU runtime patches (see modeld/egpu_patches.py).

These tests keep two promises:

* the patches work the way the eGPU stack needs (env default, firmware search order,
  USB link failures staying retryable, the sitecustomize hook actually firing);
* the patches stay OUT of upstream files - the whole point of egpu_patches.py is that
  merging carrot-wip cannot conflict on them. If a patch is ever added back to one of
  those files, the last test fails and points at the right place.
"""
import os
import subprocess
import sys
import types
from hashlib import sha256

import pytest

from openpilot.common.basedir import BASEDIR
from openpilot.selfdrive.modeld import egpu_patches


@pytest.fixture(autouse=True)
def _no_global_env_leak(monkeypatch):
  monkeypatch.delenv("AM_POWER_LIMIT", raising=False)
  yield
  monkeypatch.delenv("AM_POWER_LIMIT", raising=False)


def _stub_helpers():
  module = types.ModuleType("stub_tinygrad_helpers")
  module.fetch_fw = lambda path, name, sha256_hex: b"original"
  return module


def test_env_default_sets_the_chestnut_power_cap(monkeypatch):
  assert egpu_patches.apply_env_defaults() is True
  assert os.environ["AM_POWER_LIMIT"] == egpu_patches.AM_POWER_LIMIT
  # Already set: a later call must not change it back.
  monkeypatch.setenv("AM_POWER_LIMIT", "90")
  assert egpu_patches.apply_env_defaults() is False
  assert os.environ["AM_POWER_LIMIT"] == "90"


def test_fetch_fw_searches_local_dirs_before_the_original(monkeypatch, tmp_path):
  blob = b"firmware blob"
  firmware_dir = tmp_path / "amdgpu"
  firmware_dir.mkdir()
  (firmware_dir / "smu_14_0_2.bin.zst").write_bytes(blob)
  monkeypatch.setattr(egpu_patches, "firmware_bases", lambda: (str(tmp_path),))
  monkeypatch.setattr(egpu_patches, "_decompress", lambda raw: raw)  # no zstandard needed

  helpers = _stub_helpers()
  assert egpu_patches.patch_fetch_fw(helpers) is True
  # A local blob with the pinned hash wins, even though the original would have run.
  assert helpers.fetch_fw("amdgpu", "smu_14_0_2.bin", sha256(blob).hexdigest()) == blob
  # A miss (unknown hash, unknown name) falls through to tinygrad's own logic.
  assert helpers.fetch_fw("amdgpu", "smu_14_0_2.bin", "0" * 64) == b"original"
  assert helpers.fetch_fw("amdgpu", "other.bin", sha256(blob).hexdigest()) == b"original"
  # Idempotent: a second pass is a no-op, so double-patching cannot nest wrappers.
  assert egpu_patches.patch_fetch_fw(helpers) is False


def test_usb_link_failures_stay_retryable(monkeypatch):
  seen: list[tuple[str, object]] = []
  module = types.ModuleType("stub_precompiled_model")
  module.record_failure = lambda path, error, phase: (seen.append((str(error), phase)), True)[1]
  assert egpu_patches.patch_record_failure(module) is True

  module.record_failure("p", RuntimeError("bulk IN transfer failed"), "inference")
  detail, phase = seen[-1]
  assert "precompiled eGPU worker timed out" in detail  # the wording upstream treats as transient
  assert "bulk IN transfer failed" in detail            # while keeping the real reason
  assert phase == "inference"

  module.record_failure("p", RuntimeError("Failed to acquire lock file am_usb:4-2.lock"), "load")
  assert "precompiled eGPU worker timed out" in seen[-1][0]

  # Unrelated failures must keep their meaning: a broken artifact stays rejected.
  module.record_failure("p", RuntimeError("non-finite model output"), "inference")
  assert "precompiled eGPU worker timed out" not in seen[-1][0]
  assert egpu_patches.patch_record_failure(module) is False


def test_patch_after_import_runs_once_at_import(monkeypatch, tmp_path):
  (tmp_path / "stub_late_module.py").write_text("VALUE = 41\n")
  monkeypatch.syspath_prepend(str(tmp_path))
  fired: list[int] = []
  assert egpu_patches.patch_after_import("stub_late_module", lambda module: fired.append(module.VALUE) or True) is True
  assert fired == []
  import stub_late_module  # noqa: PLC0415 - imported on purpose after the hook is armed

  assert stub_late_module.VALUE == 41
  assert fired == [41]


def test_patch_after_import_handles_an_already_imported_module():
  module = types.ModuleType("stub_preloaded_module")
  module.mark = "pre"
  sys.modules["stub_preloaded_module"] = module
  try:
    fired: list[str] = []
    assert egpu_patches.patch_after_import("stub_preloaded_module", lambda m: fired.append(m.mark) or True) is True
    assert fired == ["pre"]
  finally:
    sys.modules.pop("stub_preloaded_module", None)


@pytest.mark.skipif(os.environ.get("OPENPILOT_EGPU_PATCH") == "0", reason="patches disabled by the environment")
def test_sitecustomize_applies_the_patches_in_a_new_process():
  """The hook has to fire without anyone importing it - that is why it exists."""
  code = "import os; print(os.environ.get('AM_POWER_LIMIT', 'unset'))"
  env = {**os.environ, "PYTHONPATH": str(BASEDIR)}
  env.pop("AM_POWER_LIMIT", None)
  result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(BASEDIR), env=env, check=True)
  assert result.stdout.strip() == egpu_patches.AM_POWER_LIMIT


def test_patches_are_not_added_back_to_upstream_files():
  """These patches must live in egpu_patches.py only (see the module docstring)."""
  modeld = (BASEDIR / "openpilot/selfdrive/modeld/modeld.py").read_text()
  compile_modeld = (BASEDIR / "openpilot/selfdrive/modeld/compile_modeld.py").read_text()
  precompiled_model = (BASEDIR / "openpilot/selfdrive/modeld/precompiled_model.py").read_text()
  usbgpu = (BASEDIR / "openpilot/system/hardware/usbgpu.py").read_text()
  launch_env = (BASEDIR / "launch_env.sh").read_text()

  assert "AM_POWER_LIMIT" not in modeld
  assert "AM_POWER_LIMIT" not in launch_env
  assert "_FW_BASES" not in compile_modeld
  assert "chestnut" not in compile_modeld
  assert "USB_TRANSIENT_TEXT" not in precompiled_model
  assert "import openpilot.selfdrive.modeld.compile_modeld" not in usbgpu
