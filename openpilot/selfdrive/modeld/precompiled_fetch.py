"""Fetch the precompiled artifacts as soon as the ONNX is in place.

build.py runs earlier in the launch sequence and skips the precompiled fetch
while the ONNX is still missing, which used to leave it until the next boot.
Calling this right after ensure_big_model() makes one boot enough.

Kept separate from big_model.py so upstream refactors of that script cannot
clobber the logic.
"""

import json
import sys
from pathlib import Path

# Touched once the download is complete, and read by the HUD badge to show
# "REBOOT". /tmp is a tmpfs: a reboot (or power loss) wipes it, so the prompt
# disappears on its own. Timestamps cannot be used for this - the clock is
# reset across power cycles.
REBOOT_FLAG = Path("/tmp/carrot-big-model-needs-reboot")

# The stock model_runtime.py only looks for AMD firmware in /lib/firmware, which is
# read-only on AGNOS. firmware.py downloads the blobs to /data/media/0/carrot/firmware,
# so without this patch tinygrad cannot find them, falls back to downloading from
# gitlab (403), and the boot validation then rejects the whole artifact set.
# Install.sh applies the same patch; this is the equivalent for the HTTP path.
_FW_OLD = '''  def fetch_fw(path, name, sha256):
    p = pathlib.Path(f"/lib/firmware/{path}/{name}.zst")
    if p.is_file():
      blob = zstandard.ZstdDecompressor().stream_reader(p.read_bytes()).read()
      if hashlib.sha256(blob).hexdigest() == sha256:
        return blob
    return _orig(path, name, sha256)'''

_FW_NEW = '''  # Firmware search path: read-only system dir, the persistent /data dir and the blobs
  # shipped in-tree for this board. This file is extracted into the model cache, so the
  # checkout cannot be located from __file__: probe both layouts it may have.
  import pathlib
  _FW_BASES = ["/lib/firmware", "/data/media/0/carrot/firmware"]
  for _cand in ("/data/openpilot/system/hardware/chestnut/firmware",
                "/data/openpilot/openpilot/system/hardware/chestnut/firmware"):
    if pathlib.Path(_cand).is_dir() and _cand not in _FW_BASES:
      _FW_BASES.append(_cand)
  try:
    from openpilot.common.basedir import BASEDIR
    _FW_ROOT = pathlib.Path(BASEDIR)
    for _cand in (_FW_ROOT / 'system' / 'hardware' / 'chestnut' / 'firmware',
                  _FW_ROOT / 'openpilot' / 'system' / 'hardware' / 'chestnut' / 'firmware'):
      if _cand.is_dir() and str(_cand) not in _FW_BASES:
        _FW_BASES.append(str(_cand))
  except Exception:
    pass
  _FW_BASES = tuple(_FW_BASES)
  def fetch_fw(path, name, sha256):
    for base in _FW_BASES:
      p = pathlib.Path(base) / path / (name + ".zst")
      if p.is_file():
        blob = zstandard.ZstdDecompressor().stream_reader(p.read_bytes()).read()
        if hashlib.sha256(blob).hexdigest() == sha256:
          return blob
    return _orig(path, name, sha256)'''

# The generic (Cinque v3) runtime ships plain tinygrad instead of model_runtime.py, so
# there is no `_orig` to replace: append a wrapper around the fetch_fw it defines. The
# wrapper is appended at the end of helpers.py (after the definition) and marked, so a
# second pass is a no-op. Everything is guarded: if the wrapper cannot import what it
# needs it falls through to the original function, i.e. to upstream's behaviour.
_FW_HELPERS_WRAPPER = '''

# --- fork patch: AMD firmware search paths -------------------------------------
# Appended by precompiled_fetch.patch_runtime_firmware_paths(). This runtime only looks
# in /lib/firmware (the AGNOS image ships a different linux-firmware revision than the
# one this runtime pins) and otherwise downloads from gitlab, which the device cannot
# reach (403) - so the eGPU fails to initialise even though the model is valid. Look
# where firmware.ensure_firmware() stores the verified blobs and, when present, at the
# blobs shipped in-tree for this board.
if not globals().get('_FORK_FW_WRAPPED'):
  import hashlib as _FORK_HS
  import pathlib as _FORK_PL
  _FORK_FW_BASES = ['/lib/firmware', '/data/media/0/carrot/firmware']
  # The checkout path is not derivable from inside an extracted runtime, so probe both
  # layouts the in-tree blobs may live at (same candidates as firmware.py).
  for _FORK_CAND in ('/data/openpilot/system/hardware/chestnut/firmware',
                     '/data/openpilot/openpilot/system/hardware/chestnut/firmware'):
    if _FORK_PL.Path(_FORK_CAND).is_dir() and _FORK_CAND not in _FORK_FW_BASES:
      _FORK_FW_BASES.append(_FORK_CAND)
  try:
    from openpilot.common.basedir import BASEDIR as _FORK_BASEDIR
    _FORK_ROOT = _FORK_PL.Path(_FORK_BASEDIR)
    for _FORK_CAND in (str(_FORK_ROOT / 'system' / 'hardware' / 'chestnut' / 'firmware'),
                       str(_FORK_ROOT / 'openpilot' / 'system' / 'hardware' / 'chestnut' / 'firmware')):
      if _FORK_PL.Path(_FORK_CAND).is_dir() and _FORK_CAND not in _FORK_FW_BASES:
        _FORK_FW_BASES.append(_FORK_CAND)
  except Exception:
    pass
  _FORK_FW_ORIG = fetch_fw

  def fetch_fw(path, name, sha256):  # noqa: F811 - intentional wrapper around upstream's
    for _base in _FORK_FW_BASES:
      _p = _FORK_PL.Path(_base) / path / (name + '.zst')
      if not _p.is_file():
        continue
      _blob = None
      try:
        import zstandard as _FORK_ZSTD
        _blob = _FORK_ZSTD.ZstdDecompressor().stream_reader(_p.read_bytes()).read()
      except Exception:
        try:
          from compression.zstd import decompress as _FORK_DZSTD
          _blob = _FORK_DZSTD(_p.read_bytes())
        except Exception:
          continue
      if _FORK_HS.sha256(_blob).hexdigest() == sha256:
        return _blob
    return _FORK_FW_ORIG(path, name, sha256)

  _FORK_FW_WRAPPED = True
'''

# The generic (Cinque v3) runtime talks to the dock's bridge from two sides at once: the AMD
# driver submits work asynchronously through tinygrad's HCQ ring, while tinygrad's own USB
# backend issues synchronous control transfers for the very same MMIO window. The bridge keeps
# its SuperSpeed link state across a C3-only reboot, so after one the first python-side access
# lands on top of the previous host's still-live submits and the link wedges: the host never
# re-enumerates the GPU, and the eGPU stays "not detected" until the dock is power-cycled.
# Draining the ring before each MMIO access serialises the two users. Upstream never sees this
# because upstream never keeps a powered dock attached across a reboot.
# patch_runtime_usb_link() applies this to the extracted runtime on every boot; the same text is
# baked into the model mirror's precompiled-runtime.tar.gz whenever that artifact is rebuilt (the
# procedure is in docs/carrot_egpu_usb_c3_reboot.md), so keep the two in sync - the mirror's
# precompiled.json carries the runtime sha256 and size of whatever is uploaded.
_USB_LINK_MARKER = 'one ring, one driver on the link'
_USB_LINK_DRAIN = f'    Device[HCQ_RUNTIME_DEV.value].synchronize()  # fork patch: {_USB_LINK_MARKER}\n'
_USB_LINK_PATCHES = (
  # The drain needs Device; the import list of this file is stable across runtime revisions.
  ('from tinygrad.device import Buffer, BufferSpec\n',
   'from tinygrad.device import Buffer, BufferSpec, Device\n'),
  # Every register access of a USB-attached AMD slice goes through these two methods.
  ('  def __getitem__(self, index):\n    off, sz = self._off_from_index(index)\n',
   '  def __getitem__(self, index):\n' + _USB_LINK_DRAIN + '    off, sz = self._off_from_index(index)\n'),
  ('  def __setitem__(self, index, data):\n    off, _ = self._off_from_index(index)\n',
   '  def __setitem__(self, index, data):\n' + _USB_LINK_DRAIN + '    off, _ = self._off_from_index(index)\n'),
)


def _installed_runtime_dir(model=None, cache_dir=None) -> Path | None:
  """The extracted runtime directory of the artifact installed for `model`, or None.

  Same key the worker uses (precompiled_model.installed): the cache holds one directory
  per artifact, so the runtime to patch is the one installed.json names.
  """
  try:
    from openpilot.selfdrive.modeld.big_model import active_manifest, model_cache_dir

    m = model or active_manifest()
    if m is None:
      return None
    root = (cache_dir or model_cache_dir()) / 'precompiled' / m.sha256
    info = json.loads((root / 'installed.json').read_text(encoding='utf-8'))
    return root / info['runtime_directory']
  except Exception:
    return None


def patch_runtime_usb_link(model=None, cache_dir=None, runtime_dir: Path | None = None) -> bool:
  """Serialise this runtime's own USB transfers against the AMD ring (see _USB_LINK_PATCHES).

  Runs on every boot, so a device that already downloaded the artifact is fixed in place
  instead of waiting for a new runtime from the mirror.
  """
  runtime = runtime_dir or _installed_runtime_dir(model, cache_dir)
  if runtime is None:
    return False
  try:
    target = runtime / 'tinygrad' / 'runtime' / 'support' / 'usb.py'
    src = target.read_text(encoding='utf-8')
    if _USB_LINK_MARKER in src:
      return True
    if not all(old in src for old, _new in _USB_LINK_PATCHES):
      from openpilot.common.swaglog import cloudlog
      cloudlog.warning('eGPU usb link patch: unexpected usb.py, skipped')
      return False
    for old, new in _USB_LINK_PATCHES:
      src = src.replace(old, new, 1)
    # Bytes, not text: write_text would translate newlines on a non-POSIX caller (the
    # packaging tool), and the artifact must stay byte-identical to the patched runtime.
    target.write_bytes(src.encode('utf-8'))
    return True
  except Exception:
    return False


def patch_runtime_firmware_paths(model=None, cache_dir=None, runtime_dir: Path | None = None) -> bool:
  """Teach the extracted runtime to look for firmware under /data as well.

  Two layouts are handled: model_runtime.py (the run-model runtime, replaced in place)
  and the generic runtime that ships plain tinygrad/ only (helpers.py, wrapped).
  `runtime_dir` is for offline callers (the packaging tool) that pass the directory
  instead of a model whose cache holds it.
  """
  try:
    runtime = runtime_dir or _installed_runtime_dir(model, cache_dir)
    if runtime is None:
      return False

    target = runtime / 'model_runtime.py'
    if target.is_file():
      src = target.read_text(encoding='utf-8')
      if '_FW_BASES' in src:
        return True
      if _FW_OLD not in src:
        from openpilot.common.swaglog import cloudlog
        cloudlog.warning('firmware path patch: unexpected model_runtime.py, skipped')
        return False
      target.write_bytes(src.replace(_FW_OLD, _FW_NEW).encode('utf-8'))
      return True

    helpers = runtime / 'tinygrad' / 'helpers.py'
    if helpers.is_file():
      src = helpers.read_text(encoding='utf-8')
      if '_FORK_FW_WRAPPED' in src:
        return True
      helpers.write_bytes((src + _FW_HELPERS_WRAPPER).encode('utf-8'))
      return True

    return False
  except Exception:
    return False


def _precompiled_rejected(manifest) -> bool:
  """Was this model's precompiled artifact rejected on this device?

  precompiled_model.reject() writes the marker after a permanent boot-validation failure.
  For a precompiled-only model (the generic PKL that Cinque v3 ships as) that is the only
  possible writer: sync_precompiled_marker() only runs once a chunked set was installed,
  and such a model can never have one. So for those models the marker always means "this
  artifact does not run on this hardware", and clearing it would only retry a known-bad
  runtime on every boot.
  """
  try:
    from openpilot.selfdrive.modeld.big_model import model_cache_dir
    return (Path(model_cache_dir()) / 'precompiled' / manifest.sha256 / 'rejected').is_file()
  except Exception:
    return False


def fetch_after_onnx(manifest, reporter) -> None:
  """Download the big-model artifacts for `manifest`, reporting progress.

  Never raises: a failure here must not prevent the normal internal-GPU build.

  Both deliveries download here, in the background, for the same reason: the boot build
  must not sit on a multi-minute download, and the HUD/web pages get their progress from
  status.json either way.
  """
  source = "auto"
  fetched_chunked = False
  newly_installed = False
  # A precompiled-only model *is* the model file: it ships no ONNX to compile locally and
  # no chunked set to split, so the precompiled artifact is the only delivery that can ever
  # produce it. A pinned "chunked" therefore cannot be honoured for it - honouring it would
  # download the model and then leave it unusable - and the pin keeps deciding the delivery
  # of every model that does have a chunked alternative.
  only_precompiled = bool(getattr(manifest, 'precompiled_only', False))
  try:
    from openpilot.selfdrive.modeld import chunked_model

    source = chunked_model.read_model_source()
    if source != "precompiled" and not only_precompiled:
      def chunked_progress(done, total) -> None:
        # detail="chunked model" maps to the same "PKL" label as the precompiled set.
        reporter.update("downloading", model_id=manifest.model_id, sha256=manifest.sha256,
                        downloaded_bytes=done, total_bytes=total, detail="chunked model")

      # A reboot is only needed when something actually landed in this run: this function
      # runs on every boot, and touching the flag unconditionally would leave the "REBOOT"
      # prompt on screen forever.
      was_installed = chunked_model.installed_chunked(manifest) is not None
      fetched_chunked = chunked_model.ensure_chunked(manifest, progress=chunked_progress) is not None
      newly_installed = fetched_chunked and not was_installed
  except Exception as exc:
    print(f"chunked model fetch deferred: {exc}", file=sys.stderr)

  if fetched_chunked:
    # Steer the (untouched) resolution to the chunked set and prompt for the reboot that
    # makes modeld pick it up.
    try:
      chunked_model.sync_precompiled_marker(manifest)
      if newly_installed:
        REBOOT_FLAG.touch()
    except Exception:
      pass  # purely cosmetic; never fail over a flag file
    return
  if source == "chunked" and not only_precompiled:
    return  # the user pinned this delivery; the boot build falls back to the local compile
  if only_precompiled and _precompiled_rejected(manifest):
    # Say why instead of leaving the HUD on "ready": there is nothing to fetch, and
    # ensure_precompiled() would skip the download for the same reason.
    reporter.update("error", model_id=manifest.model_id, sha256=manifest.sha256,
                    detail="precompiled runtime was rejected on this device")
    return
  if source == "auto" or only_precompiled:
    # Chunked is unavailable: let the precompiled artifact count as installed again.
    try:
      chunked_model.clear_precompiled_marker(manifest)
    except Exception:
      pass

  try:
    from openpilot.selfdrive.modeld.firmware import ensure_firmware
    from openpilot.selfdrive.modeld.precompiled_model import ensure_precompiled

    def precompiled_progress(done, total) -> None:
      # detail="pkl" is what the HUD badge keys off to label this phase "PKL"
      # instead of "ONNX" (the ONNX download is reported by big_model itself and
      # carries no detail field).
      reporter.update("downloading", model_id=manifest.model_id, sha256=manifest.sha256,
                      downloaded_bytes=done, total_bytes=total, detail="pkl")

    # Firmware is tiny and must be on disk before the first GPU init of the boot.
    # Upstream's ensure_precompiled used to fetch it; this fork keeps the call in
    # this fork-only module so precompiled_model.py stays upstream's file.
    ensure_firmware(manifest)
    ensure_precompiled(manifest, progress=precompiled_progress)
    # The runtime is extracted by now: teach it where the firmware lives, and serialise its
    # USB transfers against the ring (see the _USB_LINK_PATCHES block comment above). Both
    # patches are idempotent, so they are re-applied on every boot.
    patch_runtime_firmware_paths(manifest)
    patch_runtime_usb_link(manifest)
    try:
      REBOOT_FLAG.touch()
    except Exception:
      pass  # purely cosmetic; never fail over a flag file
  except Exception as exc:
    print(f"precompiled model fetch deferred: {exc}", file=sys.stderr)
