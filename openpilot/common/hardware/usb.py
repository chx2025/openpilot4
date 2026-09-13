import os
import re
from pathlib import Path

CHESTNUT_FW_VERSION = "ed4e39b7"
CHESTNUT_OFFICIAL_USB_IDS = ((0xADD1, 0x0001), (0x3801, 0x0001))
UT3G_DUAL_USB_IDS = ((0xADD1, 0x0002),)
CHESTNUT_USB_IDS = CHESTNUT_OFFICIAL_USB_IDS + UT3G_DUAL_USB_IDS
CHESTNUT_ROM_USB_IDS = ((0x174C, 0x2464), (0x174C, 0x2463))
UT3G_DUAL_PRODUCT_RE = re.compile(r"custom [0-9a-f]{8}-UT3G-DUAL")
USB_DEVICES_PATH = Path("/sys/bus/usb/devices")
TYPEC_CC_ORIENTATION_PATH = Path("/sys/class/power_supply/usb/typec_cc_orientation")
PRIMARY_USB_CONTROLLER = "a600000.ssusb"

# A chestnut eGPU that enumerates at less than USB 3.0 SuperSpeed (5 Gbps)
# has not finished its PCIe/USB link handshake. Tinygrad will try to load
# ~70 MB of model weights over a 480 Mbps (USB 2.0) or 12 Mbps (USB 1.1) pipe
# and either time out or OOM, so we must treat it as not-yet-present. The
# matching `ChestnutLinkNegotiator` in hardwared.py will poke the ASM link_up()
# ioctl on slow enumerations to redo the handshake; this constant is the
# gate the negotiator is racing against.
USB_SUPERSPEED_MIN_MBPS = 5000

# hardwared.py: how aggressively to retry the ASM link_up() ioctl when a
# chestnut device is seen at low speed. SP boot on C3XL takes ~60-90s; the
# 12V power-on race with USB-C enumeration can keep the link at USB 2.0 for
# the entire boot window, so we keep retrying for ~2 minutes before giving up
# and letting the speed gate above force the small-model fallback.
CHESTNUT_LINK_RETRY_INTERVAL_S = 5.0
CHESTNUT_LINK_RETRY_BUDGET = 24  # 24 * 5s = 120s total


def is_chestnut_runtime_device(device: dict) -> bool:
  usb_id = (int(device.get("vendorId", 0)), int(device.get("productId", 0)))
  product = str(device.get("product", ""))
  if usb_id in CHESTNUT_OFFICIAL_USB_IDS:
    return product == f"custom {CHESTNUT_FW_VERSION}-CLEAN"
  if usb_id in UT3G_DUAL_USB_IDS:
    return str(device.get("manufacturer", "")) == "tiny" and UT3G_DUAL_PRODUCT_RE.fullmatch(product) is not None
  return False


def is_chestnut_superspeed(device: dict) -> bool:
  """True iff the device is a chestnut eGPU AND has finished its USB 3.0
  SuperSpeed handshake. The 12V power-on race with USB-C enumeration can leave
  the device stuck at 480 Mbps; modeld must not try to load weights through a
  USB 2.0 pipe (see USB_SUPERSPEED_MIN_MBPS)."""
  return is_chestnut_runtime_device(device) and int(device.get("speedMbps", 0)) >= USB_SUPERSPEED_MIN_MBPS


def chestnut_runtime_present(devices: list[dict]) -> bool:
  return any(is_chestnut_runtime_device(device) for device in devices)


def chestnut_official_flash_mismatch(devices: list[dict]) -> bool:
  """Return whether comma's updater owns and needs to update a device.

  UT3G dual has a separate PID and is deliberately outside this persistent
  write domain. Factory 2065 devices remain outside it as before.
  """
  return any((int(d.get("vendorId", 0)), int(d.get("productId", 0))) in CHESTNUT_OFFICIAL_USB_IDS + CHESTNUT_ROM_USB_IDS and
             str(d.get("product", "")) != f"custom {CHESTNUT_FW_VERSION}-CLEAN" for d in devices)


def get_usb_topology() -> set[str]:
  try:
    return set(os.listdir(USB_DEVICES_PATH))
  except OSError:
    return set()


def read(path: Path) -> str | None:
  try:
    return path.read_text().strip()
  except OSError:
    return None


def read_int(path: Path, base: int = 10) -> int:
  try:
    return int(path.read_text(), base)
  except (OSError, ValueError, TypeError):
    return 0


def usb_devices() -> list[Path]:
  try:
    devices = (d for d in USB_DEVICES_PATH.glob("*") if (d / "idVendor").exists())
    return sorted(devices, key=lambda p: p.name)
  except OSError:
    return []


def controller(device: Path) -> Path | None:
  try:
    return next((parent for parent in device.resolve().parents if parent.name.endswith(".ssusb")), None)
  except OSError:
    return None


def get_usb_state() -> list[dict]:
  devices = []
  typec_orientation = read_int(TYPEC_CC_ORIENTATION_PATH)
  for device in usb_devices():
    vendor_id = read_int(device / "idVendor", 16)
    product_id = read_int(device / "idProduct", 16)
    ctrl = controller(device)
    devices.append({
      "busnum": read_int(device / "busnum"),
      "devnum": read_int(device / "devnum"),
      "vendorId": vendor_id,
      "productId": product_id,
      "speedMbps": read_int(device / "speed"),
      "manufacturer": read(device / "manufacturer") or "",
      "product": read(device / "product") or "",
      "linkErrorCount": read_int(ctrl / "portli", 0) & 0xFFFF if ctrl is not None else 0,
      "usb3Lane": {1: "a", 2: "b"}.get(typec_orientation, "unknown") if ctrl is not None and ctrl.name == PRIMARY_USB_CONTROLLER else "unknown",
    })
  return devices


def set_usb_state(device_state, devices: list[dict]) -> None:
  entries = device_state.usbState.init('devices', len(devices))

  chestnut_present = False
  for entry, device in zip(entries, devices, strict=True):
    entry.busnum = device["busnum"]
    entry.devnum = device["devnum"]
    entry.vendorId = device["vendorId"]
    entry.productId = device["productId"]
    entry.speedMbps = device["speedMbps"]
    entry.manufacturer = device["manufacturer"]
    entry.product = device["product"]
    entry.linkErrorCount = device["linkErrorCount"]
    entry.usb3Lane = device.get("usb3Lane", "unknown")

    # Gate chestnutPresent on SuperSpeed: a chestnut device still at USB 2.0/1.x
    # is in the middle of its PCIe link handshake. Reporting it as present here
    # would cause modeld to load the big model over a 480 Mbps pipe and hang.
    # The ChestnutLinkNegotiator in hardwared.py is concurrently poking the ASM
    # link_up() ioctl to drive the handshake; once speedMbps climbs to 5000+,
    # the next hardwared cycle flips this bit on.
    if is_chestnut_superspeed(device):
      chestnut_present = True

  device_state.chestnutPresent = chestnut_present
