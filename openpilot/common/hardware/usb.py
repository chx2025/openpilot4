import os
from pathlib import Path

CHESTNUT_FW_VERSION = "8516b768"
CHESTNUT_USB_IDS = ((0xADD1, 0x0001), (0x3801, 0x0001))
CHESTNUT_ROM_USB_IDS = ((0x174C, 0x2464), (0x174C, 0x2463))
CHESTNUT_USB_PRODUCT = f"custom {CHESTNUT_FW_VERSION}-CLEAN"
USB_DEVICES_PATH = Path("/sys/bus/usb/devices")
TYPEC_CC_ORIENTATION_PATH = Path("/sys/class/power_supply/usb/typec_cc_orientation")
PRIMARY_USB_CONTROLLER = "a600000.ssusb"


def is_chestnut_usb_id(vendor_id: int, product_id: int, include_bootloader: bool = False) -> bool:
  ids = CHESTNUT_USB_IDS + CHESTNUT_ROM_USB_IDS if include_bootloader else CHESTNUT_USB_IDS
  return (vendor_id, product_id) in ids


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


def _extcon_usb_attached() -> bool | None:
  """qcom extcon 里当前的 USB 连接位。

  多个 extcon 都带 USB 属性（usb-pdphy / SMB2 / eud），只要有一条是 1 就算连上；
  全是 0 表示当前没有插任何 USB 设备。读不到 extcon 时返回 None 让调用方兜底。
  """
  found = False
  try:
    states = list(Path('/sys/class/extcon').glob('*/state'))
  except OSError:
    return None
  for state in states:
    try:
      txt = state.read_text()
    except OSError:
      continue
    for line in txt.splitlines():
      key, _, val = line.partition('=')
      if key.strip() == 'USB':
        found = True
        if val.strip() == '1':
          return True
  return False if found else None


def cable_connected() -> bool:
  # [port-egpu-usb] TYPEC_CC_ORIENTATION 是粘滞值：线拔掉后内核不清零
  # （实测拔掉底座后仍是 2）。只看它会让 CHESTNUT 分支永远成立 -> 每次 onroad 都要
  # 白等 wait_for_chestnut() 超时，然后置 ChestnutRetry=1，下次也不再自动重试。
  # 优先用 extcon 的 USB 连接位判断「现在到底插没插」。
  attached = _extcon_usb_attached()
  if attached is not None:
    return attached
  return read_int(TYPEC_CC_ORIENTATION_PATH) != 0


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

    if is_chestnut_usb_id(entry.vendorId, entry.productId):
      chestnut_present = True

  device_state.chestnutPresent = chestnut_present
  device_state.usbState.connected = cable_connected()
