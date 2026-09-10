import os
import threading
from collections.abc import Callable, MutableMapping


# Measured on the C3XL/UT3G device on 2026-08-29:
# LM 71.50 s, BMV3 73.54 s, TT 74.66 s, BMV2 75.58 s.
# Keep a bounded 44.42 s margin for cold starts and USB scheduling variance.
C3XL_MODEL_LOAD_TIMEOUT = 120
C3XL_TINYGRAD_CACHE_HOME = "/data/cache"
C3XL_AM_POWER_LIMIT_W = 100
C3XL_AMD_USB_POLL_US = 500


class EgpuModelLoadError(RuntimeError):
  pass


def load_with_progress(loader, stream, *, total_size: int | None = None, progress_callback=None):
  """Track I/O around the official loader without changing its pickle semantics."""
  class ProgressReader:
    bytes_read = 0

    def report(self, count):
      self.bytes_read += count
      if progress_callback is not None and total_size:
        progress_callback(min(1.0, self.bytes_read / total_size))

    def read(self, size=-1):
      data = stream.read(size)
      self.report(len(data))
      return data

    def readinto(self, buffer):
      view = memoryview(buffer).cast('B')
      offset = 0
      while offset < len(view):
        count = stream.readinto(view[offset:])
        if not count:
          raise EOFError('truncated out-of-band pickle buffer')
        offset += count
        self.report(count)
      return offset

  result = loader(ProgressReader())
  if progress_callback is not None:
    progress_callback(1.0)
  return result


def configure_default_device(comma_hardware: bool, environment: MutableMapping[str, str] = os.environ, *, c3xl: bool = False) -> None:
  """Prevent tinygrad's default-device scan from probing the USB AMD GPU."""
  if comma_hardware:
    environment.setdefault("DEV", "QCOM")
    # Cap the AMD SMU PPT unconditionally on comma hardware. tinygrad's USB+AMD
    # backend (AMDev) reads AM_POWER_LIMIT and calls smu.set_power_limit(); without
    # it the GPU runs at full PPT and an inference spike trips the USB PD supply
    # (the "loads fine, dies on first run" symptom). Do NOT gate this on the C3XL
    # hardware-profile detection -- that detection has proven unreliable on some
    # units, and setting the env var is harmless when no AMD GPU is attached
    # (AMDev is never initialized on the QCOM/LLVM path). An explicit environment
    # override remains available for controlled testing.
    environment.setdefault("AM_POWER_LIMIT", str(C3XL_AM_POWER_LIMIT_W))
  if c3xl:
    # /home is an ephemeral overlay on C3XL. Keep AMD firmware and compiler
    # caches across reboots so model startup never depends on a live download.
    environment.setdefault("XDG_CACHE_HOME", C3XL_TINYGRAD_CACHE_HOME)
    # Follow the SP/OP Chestnut default. Keep tinygrad's other devices unchanged.
    environment.setdefault("AMD_USB_POLL_US", str(C3XL_AMD_USB_POLL_US))


def load_with_timeout[T](load: Callable[[], T], timeout: float) -> T:
  result: list[T] = []
  error: list[Exception] = []
  done = threading.Event()

  def run() -> None:
    try:
      result.append(load())
    except Exception as e:
      error.append(e)
    finally:
      done.set()

  threading.Thread(target=run, name="egpu-model-loader", daemon=True).start()
  if not done.wait(timeout):
    raise TimeoutError(f"eGPU model load timed out after {timeout:g}s")
  if error:
    raise EgpuModelLoadError(f"eGPU model load failed: {error[0]}") from error[0]
  return result[0]
