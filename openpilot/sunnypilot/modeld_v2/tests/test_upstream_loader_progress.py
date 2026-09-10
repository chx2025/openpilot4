import io

import numpy as np
import pytest

from openpilot.selfdrive.modeld.helpers import dump_oob
from openpilot.sunnypilot.modeld_v2.egpu_loader import load_with_progress
from openpilot.sunnypilot.modeld_v2.helpers import load_oob


class FragmentedReader(io.BytesIO):
  def readinto(self, b):
    return super().readinto(memoryview(b)[:4096])


def model_bytes():
  stream = io.BytesIO()
  value = {'metadata': {'name': 'test'}, 'weights': np.arange(65536, dtype=np.float32)}
  dump_oob(value, stream)
  return value, stream.getvalue()


@pytest.mark.parametrize('reader', [io.BytesIO, FragmentedReader])
def test_official_loader_preserves_buffers_and_progress(reader):
  expected, raw = model_bytes()
  progress = []
  actual = load_with_progress(load_oob, reader(raw), total_size=len(raw), progress_callback=progress.append)
  assert actual['metadata'] == expected['metadata']
  np.testing.assert_array_equal(actual['weights'], expected['weights'])
  assert progress == sorted(progress)
  assert all(0.0 <= value <= 1.0 for value in progress)
  assert progress[-1] == 1.0


def test_truncated_buffer_fails_without_reporting_completion():
  _, raw = model_bytes()
  progress = []
  with pytest.raises(EOFError, match='truncated'):
    load_with_progress(load_oob, FragmentedReader(raw[:-100]), total_size=len(raw), progress_callback=progress.append)
  assert progress and progress[-1] < 1.0


def test_official_loader_is_called_once_and_exceptions_are_not_retried():
  calls = []
  progress = []
  def loader(stream):
    calls.append(stream.read(1))
    raise ValueError('bad model')
  with pytest.raises(ValueError, match='bad model'):
    load_with_progress(loader, io.BytesIO(b'ab'), total_size=2, progress_callback=progress.append)
  assert calls == [b'a']
  assert progress == [0.5]
