from pathlib import Path

from openpilot.selfdrive.modeld import helpers


def make_usb(tmp_path: Path, vid: str, pid: str, manufacturer: str, product: str, speed_mbps: str = "5000") -> Path:
  device = tmp_path / "4-1"
  device.mkdir()
  for name, value in (("idVendor", vid), ("idProduct", pid), ("manufacturer", manufacturer), ("product", product), ("speed", speed_mbps)):
    (device / name).write_text(value)
  return device


def test_chestnut_present_accepts_official_and_dual(monkeypatch, tmp_path):
  monkeypatch.setattr(helpers, "USB_DEVICES_PATH", tmp_path)
  make_usb(tmp_path, "add1", "0002", "tiny", "custom d1377a01-UT3G-DUAL")
  assert helpers.chestnut_present()


def test_chestnut_present_rejects_dirty_dual(monkeypatch, tmp_path):
  monkeypatch.setattr(helpers, "USB_DEVICES_PATH", tmp_path)
  make_usb(tmp_path, "add1", "0002", "tiny", "custom d1377a01-UT3G-DUAL-DIRTY")
  assert not helpers.chestnut_present()


def test_chestnut_present_rejects_factory(monkeypatch, tmp_path):
  monkeypatch.setattr(helpers, "USB_DEVICES_PATH", tmp_path)
  make_usb(tmp_path, "2065", "2463", "ASMedia", "ASM246X series")
  assert not helpers.chestnut_present()


def test_chestnut_present_rejects_usb2_enumeration(monkeypatch, tmp_path):
  """A chestnut eGPU that enumerated at USB 2.0 (480 Mbps) is mid-handshake;
  the speed gate must keep it invisible to modeld so the big model is not
  loaded over a USB 2.0 pipe."""
  monkeypatch.setattr(helpers, "USB_DEVICES_PATH", tmp_path)
  make_usb(tmp_path, "add1", "0002", "tiny", "custom d1377a01-UT3G-DUAL", speed_mbps="480")
  assert not helpers.chestnut_present()


def test_chestnut_present_rejects_usb1_enumeration(monkeypatch, tmp_path):
  """12 Mbps (USB 1.1 full-speed) is the other common low-speed enumeration
  outcome of the 12V / USB-C power race; same gating applies."""
  monkeypatch.setattr(helpers, "USB_DEVICES_PATH", tmp_path)
  make_usb(tmp_path, "add1", "0002", "tiny", "custom d1377a01-UT3G-DUAL", speed_mbps="12")
  assert not helpers.chestnut_present()


def test_chestnut_present_accepts_exactly_5000_mbps(monkeypatch, tmp_path):
  """The SuperSpeed gate is `>= 5000`, not `> 5000`, so a 5 Gbps exact
  enumeration is accepted (boundary check)."""
  monkeypatch.setattr(helpers, "USB_DEVICES_PATH", tmp_path)
  make_usb(tmp_path, "add1", "0002", "tiny", "custom d1377a01-UT3G-DUAL", speed_mbps="5000")
  assert helpers.chestnut_present()
