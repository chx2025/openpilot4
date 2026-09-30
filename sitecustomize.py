"""Fork hook: apply the USB-eGPU runtime patches in every python process.

CPython imports this module automatically at interpreter start (for ``python -c``,
``python -m`` and scripts alike) as long as this directory is on ``sys.path``. The
device launcher puts the checkout root there through ``PYTHONPATH``
(launch_chffrplus.sh), which the manager and its children inherit - including the
precompiled worker subprocess and the ``check_usbgpu`` probe. That makes this the one
place where a patch can run before user code without editing an upstream file.

Keep this file tiny and defensive: it runs in every python process on the device
(pytest and the modeld worker included), so it only delegates, and any failure is
swallowed. Set ``OPENPILOT_EGPU_PATCH=0`` to disable the patches.

See openpilot/selfdrive/modeld/egpu_patches.py for what is applied and why.
"""

import os

if os.environ.get("OPENPILOT_EGPU_PATCH", "1") != "0":
  try:
    from openpilot.selfdrive.modeld import egpu_patches

    egpu_patches.apply()
  except Exception:
    pass
