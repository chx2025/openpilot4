"""Tests for ChestnutLinkNegotiator: the per-boot state machine that drives
the ASM link_up() ioctl retry when a chestnut eGPU enumerates below USB 3.0.

The negotiator is the self-healing side of the USB-handshake fix. The
defensive side (speed-gating chestnut_present / chestnutPresent) is in
test_usbgpu_identity.py. These tests focus on:
  - throttling: a single chestnut at low speed triggers at most one poke per
    CHESTNUT_LINK_RETRY_INTERVAL_S, not on every hardwared cycle
  - bounding: total attempts per (vid, pid) per boot are capped at the budget
  - give-up: after the budget is exhausted the negotiator logs once and stops
  - don't disturb: if any chestnut device is already at SuperSpeed, no ioctl
    is sent even if a USB 2.0 sibling is visible
"""
import sys
import types

import pytest

from openpilot.common.hardware import usb
from openpilot.system.hardware import hardwared


CHESTNUT_DUAL = {
  "vendorId": 0xADD1, "productId": 0x0002, "manufacturer": "tiny",
  "product": "custom d1377a01-UT3G-DUAL", "speedMbps": 480,
}


def _install_fake_flash(monkeypatch):
  """Inject a fake `openpilot.system.hardware.chestnut.flash` module into
  sys.modules. The real module imports fcntl at top level, which is
  Linux-only and not present in the Windows test environment. The
  negotiator lazy-imports link_up from this module, so a fake is enough
  to drive every code path."""
  calls = []

  def fake_link_up():
    calls.append(("link_up", None, None))
    return True

  fake = types.ModuleType("openpilot.system.hardware.chestnut.flash")
  fake.link_up = fake_link_up

  monkeypatch.setitem(sys.modules, "openpilot.system.hardware.chestnut.flash", fake)
  return calls


def _make_negotiator(monkeypatch, fake_calls):
  """Return a fresh negotiator with link_up() and cloudlog stubbed."""
  logs = []
  monkeypatch.setattr(hardwared, "cloudlog", type("L", (), {
    "warning": lambda *a, **k: logs.append(("warning", a, k)),
    "exception": lambda *a, **k: logs.append(("exception", a, k)),
  })())
  return hardwared.ChestnutLinkNegotiator(), logs, fake_calls


def test_no_chestnut_no_ioctl(monkeypatch):
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, logs, _ = _make_negotiator(monkeypatch, fake_calls)
  negotiator.update([])
  assert fake_calls == []
  assert logs == []


def test_superspeed_chestnut_not_disturbed(monkeypatch):
  """If a chestnut device is already at 5000 Mbps, do not poke the ioctl
  even if a USB 2.0 sibling entry is also in the topology."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, _, _ = _make_negotiator(monkeypatch, fake_calls)
  negotiator.update([
    {**CHESTNUT_DUAL, "speedMbps": 480},     # USB 2.0 sibling
    {**CHESTNUT_DUAL, "speedMbps": 5000},    # USB 3.0 sibling of the same device
  ])
  assert fake_calls == []


def test_low_speed_triggers_one_ioctl(monkeypatch):
  """A chestnut at low speed triggers link_up() exactly once on the first
  call, then throttles on subsequent calls within the retry interval."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, _, _ = _make_negotiator(monkeypatch, fake_calls)
  negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == 1

  # Within CHESTNUT_LINK_RETRY_INTERVAL_S: no new calls
  negotiator.update([CHESTNUT_DUAL])
  negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == 1


def test_throttle_uses_configured_interval(monkeypatch):
  """The throttle must use CHESTNUT_LINK_RETRY_INTERVAL_S (5s), not 1s like
  the build-time compile_modeld.py loop. A 1s clock advance should still
  leave the negotiator throttled; a full interval advance should release it."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, _, _ = _make_negotiator(monkeypatch, fake_calls)
  fake_now = [1000.0]
  monkeypatch.setattr(hardwared.time, "monotonic", lambda: fake_now[0])

  negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == 1

  # 1s later: still throttled
  fake_now[0] += 1.0
  negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == 1

  # At exactly the interval boundary: released
  fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S - 1.0
  negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == 2


def test_budget_caps_total_attempts(monkeypatch):
  """After CHESTNUT_LINK_RETRY_BUDGET (24) attempts, the negotiator stops
  sending ioctls even when the device is still slow, and logs a give-up
  warning once."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, logs, _ = _make_negotiator(monkeypatch, fake_calls)
  fake_now = [1000.0]
  monkeypatch.setattr(hardwared.time, "monotonic", lambda: fake_now[0])

  # Run the full budget
  for i in range(usb.CHESTNUT_LINK_RETRY_BUDGET):
    negotiator.update([CHESTNUT_DUAL])
    assert len(fake_calls) == i + 1
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1

  # One more update: should NOT call link_up, but SHOULD emit give-up warning
  n_before = len(fake_calls)
  negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == n_before, "ioctl fired after budget exhausted"
  gave_up_logs = [c for c in logs if c[0] == "warning" and "gave up" in str(c[1])]
  assert len(gave_up_logs) == 1, f"expected one give-up log, got {gave_up_logs}"

  # Subsequent updates stay silent
  fake_now[0] += 10.0
  n_before = len(fake_calls)
  negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == n_before


def test_link_up_failure_does_not_break_negotiator(monkeypatch):
  """If the ioctl itself raises (e.g. device briefly disappeared), the
  negotiator must swallow the exception and continue trying up to the
  budget, not abort and leave modeld hanging."""
  # Override fake_link_up to raise
  def fake_link_up_raising():
    raise OSError("device briefly gone")

  fake = types.ModuleType("openpilot.system.hardware.chestnut.flash")
  calls = []
  def fake_link_up():
    calls.append(("link_up", None, None))
    raise OSError("device briefly gone")
  fake.link_up = fake_link_up
  monkeypatch.setitem(sys.modules, "openpilot.system.hardware.chestnut.flash", fake)

  logs = []
  monkeypatch.setattr(hardwared, "cloudlog", type("L", (), {
    "warning": lambda *a, **k: logs.append(("warning", a, k)),
    "exception": lambda *a, **k: logs.append(("exception", a, k)),
  })())

  negotiator = hardwared.ChestnutLinkNegotiator()
  fake_now = [1000.0]
  monkeypatch.setattr(hardwared.time, "monotonic", lambda: fake_now[0])

  for _ in range(3):
    negotiator.update([CHESTNUT_DUAL])
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1

  # Three link_up calls made, each raised but was caught (logged as exception)
  assert len(calls) == 3
  assert len([c for c in logs if c[0] == "exception"]) == 3
  # Did NOT hit give-up
  assert not any("gave up" in str(c[1]) for c in logs if c[0] == "warning")


def test_budget_covers_two_minute_boot_window():
  """The retry budget multiplied by the interval must cover the worst-case
  SP boot window on C3XL (~60-90s) with margin. 5s * 24 = 120s; assert this
  explicitly so a future tweak that breaks the budget gets caught here
  rather than as a C3XL-only boot regression."""
  assert usb.CHESTNUT_LINK_RETRY_INTERVAL_S * usb.CHESTNUT_LINK_RETRY_BUDGET >= 120


def test_per_vid_pid_budget_is_independent(monkeypatch):
  """The budget is tracked per (vid, pid), not globally. A failed device
  must not block a working second chestnut plugged in later."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, logs, _ = _make_negotiator(monkeypatch, fake_calls)
  fake_now = [1000.0]
  monkeypatch.setattr(hardwared.time, "monotonic", lambda: fake_now[0])

  official = {**CHESTNUT_DUAL, "vendorId": 0xADD1, "productId": 0x0001, "speedMbps": 480}

  # Burn the entire budget on the dual VID/PID
  for _ in range(usb.CHESTNUT_LINK_RETRY_BUDGET):
    negotiator.update([CHESTNUT_DUAL])
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1
  # Confirm we are exhausted on this key
  n_after_exhaust = len(fake_calls)
  negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == n_after_exhaust

  # Now plug in a different chestnut device; it gets a fresh budget
  negotiator.update([official])
  assert len(fake_calls) == n_after_exhaust + 1
  # The official chestnut did NOT trigger a give-up log
  assert len([c for c in logs if c[0] == "warning" and "gave up" in str(c[1])]) == 1
