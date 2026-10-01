"""Which pinned engine the comma hands the host, and where it comes from.

A model here *is* its SPEC. The sha256 inside the SPEC names the cache file on
both sides, the host builds its engine for it, and link.py validates every reply
against it - so switching models re-downloads the ONNX and re-builds the host
engine. There is nothing cheaper to switch, and this module is the single place
that decides which SPEC the rest of the jetlink code loads.

The built-in list holds the checkpoint this branch ships. A server-side catalog
(CATALOG_PATH, next to MODEL_BASE) can add more, the same way the eGPU picker
does in selfdrive/modeld/model_source.py; the picked sha256 is remembered in
INDEX so every process reads the same choice.

This module must not import link.py or mac.py: both import it, at module level,
before they can do anything.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import urllib.request

SPEC_DIR = Path(__file__).resolve().parent
BUILTIN_SPEC = 'cinque_v2.json'
DEFAULT_LABEL = 'Cinque v2'

# Where the ONNX handed to the host is kept, keyed by SPEC.sha256. LEGACY_CACHE
# holds the copy from when only the Mac app could be provisioned.
CACHE = Path('/data/models/carrot-jetlink-host')
LEGACY_CACHE = Path('/data/models/carrot-jetlink-mac')
# The choice itself. Deliberately next to the models rather than in the Params
# database, so a device with no manager running still resolves it.
INDEX = Path('/data/models/carrot-jetlink/index.json')

CATALOG_PATH = 'jetlink/catalog.json'
REMOTE_TIMEOUT = 6.0

# Resolved on first use; see selected_entry().
_RESOLVED: dict | None = None


def _builtin_url(sha256: str) -> str:
  # The layout the pinned Cinque v2 has always been published under. Kept as a
  # function so a catalog entry can override it per model.
  return ('https://upload.shind0.synology.me/models/carrot-jetlink-cinque-v2/'
          'big_driving_supercombo.onnx')


def _read_json(path: Path):
  try:
    return json.loads(path.read_text())
  except (OSError, ValueError):
    return None


def _builtin_spec() -> dict:
  spec = _read_json(SPEC_DIR / BUILTIN_SPEC)
  return spec if isinstance(spec, dict) else {}


def _builtin() -> list[dict]:
  """The checkpoint this branch ships, described from its own SPEC."""
  spec = _builtin_spec()
  sha = spec.get('sha256')
  if not isinstance(sha, str) or len(sha) != 64:
    return []
  return [{'sha256': sha, 'label': DEFAULT_LABEL, 'spec_file': BUILTIN_SPEC,
           'spec': spec, 'url': _builtin_url(sha), 'nbytes': spec.get('nbytes'),
           'builtin': True}]


def _base_url() -> str | None:
  """MODEL_BASE, imported lazily: it lives with the eGPU model plumbing."""
  try:
    from openpilot.selfdrive.modeld.model_source import MODEL_BASE
  except Exception:
    return None
  return MODEL_BASE or None


def remote_catalog() -> list[dict] | None:
  """Optional server-side list of selectable jetlink models.

  Same shape as the eGPU catalog: {"models": [{sha256, label, size, url, spec}]}
  where `spec` is either the SPEC document itself or a SPEC file name kept in
  this directory. A model whose SPEC is unavailable here cannot be used, because
  the contract has to exist on both ends.
  """
  base = _base_url()
  if not base:
    return None
  url = base if base.endswith('/') else base + '/'
  url = url + CATALOG_PATH
  try:
    request = urllib.request.Request(url, headers={'User-Agent': 'carrot-jetlink/1'})
    with urllib.request.urlopen(request, timeout=REMOTE_TIMEOUT) as response:
      if response.status != 200:
        return None
      value = json.loads(response.read().decode('utf-8'))
  except Exception:
    return None
  if not isinstance(value, dict):
    return None
  entries = value.get('models')
  if not isinstance(entries, list):
    return None
  result: list[dict] = []
  for entry in entries:
    if not isinstance(entry, dict):
      continue
    sha = entry.get('sha256')
    if not isinstance(sha, str) or len(sha) != 64:
      continue
    spec = entry.get('spec')
    spec_file = entry.get('spec_file') or entry.get('filename')
    if not isinstance(spec, dict) and isinstance(spec_file, str):
      spec = _read_json(SPEC_DIR / Path(spec_file).name)
    if not isinstance(spec, dict) or spec.get('sha256') != sha:
      # Without a matching contract the host could never agree with us.
      continue
    result.append({'sha256': sha, 'label': entry.get('label') or sha[:12],
                   'spec': spec, 'spec_file': spec_file if isinstance(spec_file, str) else None,
                   'url': entry.get('url'), 'nbytes': entry.get('nbytes') or spec.get('nbytes'),
                   'builtin': False})
  return result


def read_index() -> dict:
  value = _read_json(INDEX)
  return value if isinstance(value, dict) else {}


def write_index(selected: str | None) -> bool:
  INDEX.parent.mkdir(parents=True, exist_ok=True)
  temporary = INDEX.with_suffix('.tmp')
  try:
    temporary.write_text(json.dumps({'selected': selected}, indent=2))
    os.replace(temporary, INDEX)
  except OSError:
    temporary.unlink(missing_ok=True)
    return False
  return True


def entries(refresh: bool = True) -> list[dict]:
  """Built-in models first, then anything the catalog adds."""
  result = _builtin()
  known = {item['sha256'] for item in result}
  if refresh:
    for item in remote_catalog() or []:
      if item['sha256'] not in known:
        result.append(item)
        known.add(item['sha256'])
  return result


def entry_for(sha256: str | None, refresh: bool = True) -> dict | None:
  for item in entries(refresh):
    if item['sha256'] == sha256:
      return item
  return None


def selected_entry(refresh: bool = True) -> dict:
  """The model in use, resolved once per process.

  Switching models needs a reboot - the host has to rebuild its engine for the
  new sha256 - so a process that is already running never has to notice a
  change. Resolve on first use and keep it: daemon.publish() runs every second
  and must not touch the index each time.

  Falls back to the built-in model, never returns None.
  """
  global _RESOLVED
  if _RESOLVED is None:
    _RESOLVED = _resolve_selected(refresh)
  return _RESOLVED


def _resolve_selected(refresh: bool) -> dict:
  builtin = _builtin()
  fallback = builtin[0] if builtin else None
  sha = read_index().get('selected')
  if not isinstance(sha, str) or len(sha) != 64:
    return fallback or {}
  return entry_for(sha, refresh) or fallback or {}


def selected_sha(refresh: bool = True) -> str | None:
  return selected_entry(refresh).get('sha256')


def label(refresh: bool = True) -> str:
  """What the UI and the logs call the current model."""
  entry = selected_entry(refresh)
  return entry.get('label') or DEFAULT_LABEL


def spec_path(refresh: bool = True) -> Path:
  """Path of the SPEC the jetlink code must load.

  Always returns an existing file: a selection whose SPEC cannot be found would
  otherwise leave the device unable to describe itself on the wire.
  """
  entry = selected_entry(refresh)
  name = entry.get('spec_file')
  if isinstance(name, str):
    candidate = SPEC_DIR / Path(name).name
    if candidate.is_file():
      return candidate
  return SPEC_DIR / BUILTIN_SPEC


def model_url(refresh: bool = True) -> str:
  entry = selected_entry(refresh)
  url = entry.get('url')
  return url if isinstance(url, str) and url else _builtin_url(entry.get('sha256') or '')


def model_nbytes(refresh: bool = True) -> int | None:
  entry = selected_entry(refresh)
  size = entry.get('nbytes')
  return size if isinstance(size, int) and size > 0 else None


def cached_paths(sha256: str | None = None, refresh: bool = True) -> list[Path]:
  sha = sha256 or selected_sha(refresh)
  if not isinstance(sha, str):
    return []
  return [root / f'{sha}.onnx' for root in (CACHE, LEGACY_CACHE)]


def downloaded(sha256: str | None = None, refresh: bool = True) -> bool:
  return any(path.is_file() for path in cached_paths(sha256, refresh))


def select(sha256: str) -> bool:
  if entry_for(sha256) is None:
    return False
  return write_index(sha256)


def remove(sha256: str) -> bool:
  """Drop a cached ONNX. Refuses the model in use: its SPEC names the file."""
  if sha256 == selected_sha():
    return False
  removed = False
  for path in cached_paths(sha256):
    try:
      path.unlink()
      removed = True
    except OSError:
      pass
  return removed


def free_bytes() -> int:
  try:
    return shutil.disk_usage(CACHE.parent if not CACHE.exists() else CACHE).free
  except OSError:
    return 0
