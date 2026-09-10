from types import SimpleNamespace

import numpy as np
import pytest

from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.sunnypilot.modeld_v2.camera_offset_helper import CameraOffsetHelper


@pytest.mark.parametrize("sensor", ["ar0231", "ox03c10"])
@pytest.mark.parametrize("main_wide", [False, True])
def test_c3xl_camera_offset_preserves_each_camera_horizon(sensor, main_wide):
  helper = CameraOffsetHelper()
  pitch = np.radians(-4.0)
  cameras = DEVICE_CAMERAS[('tici', sensor)]
  sm = {
    'deviceState': SimpleNamespace(deviceType='tici'),
    'narrowRoadCameraState': SimpleNamespace(sensor=sensor),
    'extrinsicsCalibration': SimpleNamespace(rpyCalib=[0., pitch, 0.], height=[1.22]),
  }
  original = np.eye(3, dtype=np.float32)
  for result in helper.update(original, original, sm, main_wide):
    np.testing.assert_array_equal(result, original)
  helper.set_offset(0.2)
  outputs = helper.update(original, original, sm, main_wide)
  intrinsics = ((cameras.wide_road if main_wide else cameras.narrow_road).intrinsics, cameras.wide_road.intrinsics)
  for shear, k in zip(outputs, intrinsics, strict=True):
    horizon = k[1, 2] - k[1, 1] * np.tan(pitch)
    ray = np.array([k[0, 2], horizon, 1.])
    np.testing.assert_allclose(shear @ ray, ray, atol=1e-5)
    assert (shear @ (ray + [0., 100., 0.]))[0] > ray[0]
