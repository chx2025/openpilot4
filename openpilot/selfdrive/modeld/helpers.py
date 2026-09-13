import io
import json
import pickle
import shutil
import struct
import tempfile
from pathlib import Path

from openpilot.common.file_chunker import get_manifest_path
from openpilot.common.hardware.usb import USB_DEVICES_PATH, is_chestnut_superspeed, is_chestnut_runtime_device

MODELS_DIR = Path(__file__).resolve().parent / 'models'
TG_INPUT_DEVICES_PATH = MODELS_DIR / 'tg_input_devices.json'
CHESTNUT_POWERED_VOLTAGE = 5000
CHESTNUT_PCIE_READY = 0x78


def get_tg_input_devices(process_name: str, chestnut: bool):
  with open(TG_INPUT_DEVICES_PATH) as f:
    return json.load(f)[process_name]['default' if not chestnut else 'chestnut']

def modeld_pkl_path(chestnut: bool):
  prefix = 'big_' if chestnut else ''
  return MODELS_DIR / f'{prefix}driving_tinygrad.pkl'

def dump_oob(obj, f):
  with tempfile.TemporaryFile(dir=".") as tmp:
    def buffer_callback(pb: pickle.PickleBuffer):
      m = pb.raw()
      tmp.write(struct.pack('<q', m.nbytes))
      tmp.write(m)
      pb.release() # keep peak ram at ~1 buffer
    stream = io.BytesIO()
    pickle.Pickler(stream, protocol=5, buffer_callback=buffer_callback).dump(obj)
    opcodes = stream.getvalue()
    f.write(struct.pack('<q', len(opcodes)))
    f.write(opcodes)
    tmp.seek(0)
    shutil.copyfileobj(tmp, f)

def load_oob(f, *, total_size: int | None = None, progress_callback=None):
  bytes_read = 0

  def read(size: int):
    nonlocal bytes_read
    data = f.read(size)
    bytes_read += len(data)
    if progress_callback is not None and total_size:
      progress_callback(min(1.0, bytes_read / total_size))
    return data

  def readinto(view) -> int:
    nonlocal bytes_read
    count = f.readinto(view)
    if count is None:
      count = 0
    bytes_read += count
    if progress_callback is not None and total_size:
      progress_callback(min(1.0, bytes_read / total_size))
    return count

  opcodes = read(struct.unpack('<q', read(8))[0])
  def buffers():
    while (h := read(8)):
      pb = pickle.PickleBuffer(bytearray(struct.unpack('<q', h)[0]))
      view = pb.raw()
      offset = 0
      while offset < len(view):
        count = readinto(view[offset:])
        if not count:
          raise EOFError("truncated out-of-band pickle buffer")
        offset += count
      yield pb
  result = pickle.load(io.BytesIO(opcodes), buffers=buffers())
  if progress_callback is not None:
    progress_callback(1.0)
  return result

def chestnut_present() -> bool:
  for d in USB_DEVICES_PATH.glob("*"):
    try:
      device = {
        "vendorId": int((d / "idVendor").read_text(), 16),
        "productId": int((d / "idProduct").read_text(), 16),
        "manufacturer": (d / "manufacturer").read_text().strip(),
        "product": (d / "product").read_text().strip(),
        # `speed` is in Mbps and reflects the negotiated link. The 12V power-on
        # race with USB-C enumeration can leave chestnut at 480/12 Mbps; only
        # accept the device when it has reached USB 3.0 SuperSpeed (>= 5000
        # Mbps) so tinygrad doesn't try to push ~70 MB of weights over a USB
        # 2.0 pipe. The ChestnutLinkNegotiator in hardwared.py races to flip
        # the link before modeld starts, so this gate is rarely long-lived.
        "speedMbps": int((d / "speed").read_text().strip() or 0),
      }
      if is_chestnut_superspeed(device):
        return True
    except Exception:
      pass
  return False

def chestnut_compiled() -> bool:
  return Path(get_manifest_path(modeld_pkl_path(chestnut=True))).is_file()


def chestnut_ready(state) -> bool:
  return state.supplyVoltage >= CHESTNUT_POWERED_VOLTAGE and not state.supplyFault and state.pcieLtssm == CHESTNUT_PCIE_READY
