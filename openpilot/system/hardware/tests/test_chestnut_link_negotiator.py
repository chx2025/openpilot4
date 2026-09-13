"""Tests for ChestnutLinkNegotiator: the per-boot state machine that recovers a
chestnut eGPU after a bad power-on ordering.

Three ladders, all exercised here:
  - poke: a chestnut enumerated below USB 3.0 gets repeated ASM link_up() ioctls
  - escalation: failed pokes are followed by a USB VBUS power cycle
  - absent: the dock never enumerated at all (C3XL and dock switched on
    simultaneously), so there is nothing to poke and VBUS must be cycled blind

The defensive side (speed-gating chestnut_present / chestnutPresent) is in
test_usbgpu_identity.py. These tests focus on:
  - throttling / bounding / give-up of every ladder
  - don't disturb: a device already at SuperSpeed is never touched
  - recovery is not permanent: budgets release when the link heals, and a
    gave-up link is re-armed a bounded number of times (hardwared is
    long-lived, so an offroad/onroad toggle does not restart it)
  - honesty: a VBUS cycle that could not happen must not be silently treated
    as a successful recovery
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


def _install_fake_flash(monkeypatch, *, vbus_control=True, vbus_succeeds=True):
  """Inject a fake `openpilot.system.hardware.chestnut.flash` module into
  sys.modules. The real module imports fcntl at top level, which is
  Linux-only and not present in the Windows test environment. The
  negotiator lazy-imports from this module, so a fake is enough to drive
  every code path.

  Returns the list of calls made, as strings, so tests can assert on the
  ordering of pokes and VBUS cycles."""
  calls = []

  def fake_link_up():
    calls.append("link_up")
    return True

  def fake_vbus_cycle():
    if not vbus_succeeds:
      return False
    calls.append("vbus_cycle")
    return True

  fake = types.ModuleType("openpilot.system.hardware.chestnut.flash")
  fake.link_up = fake_link_up
  fake.vbus_cycle = fake_vbus_cycle
  fake.vbus_control_available = lambda: vbus_control

  monkeypatch.setitem(sys.modules, "openpilot.system.hardware.chestnut.flash", fake)
  return calls


def _stub_cloudlog(monkeypatch):
  logs = []
  monkeypatch.setattr(hardwared, "cloudlog", type("L", (), {
    "warning": lambda *a, **k: logs.append(("warning", a, k)),
    "exception": lambda *a, **k: logs.append(("exception", a, k)),
  })())
  return logs


def _freeze_clock(monkeypatch, start):
  """Freeze hardwared's clock and return the mutable time holder."""
  fake_now = [start]
  monkeypatch.setattr(hardwared.time, "monotonic", lambda: fake_now[0])
  return fake_now


def _make_negotiator(monkeypatch, fake_calls):
  """Return a fresh negotiator with cloudlog stubbed (clock left untouched)."""
  return hardwared.ChestnutLinkNegotiator(), _stub_cloudlog(monkeypatch), fake_calls


def _make_negotiator_frozen(monkeypatch, start=1000.0):
  """Return a negotiator whose construction time and clock are frozen, so the
  absent-device grace period is deterministic. The clock must be frozen before
  construction because the negotiator samples time.monotonic() in __init__."""
  fake_now = _freeze_clock(monkeypatch, start)
  logs = _stub_cloudlog(monkeypatch)
  return hardwared.ChestnutLinkNegotiator(), logs, fake_now


def _warnings(logs, needle):
  return [c for c in logs if c[0] == "warning" and needle in str(c[1])]


# --------------------------------------------------------------------- baseline
def test_no_chestnut_no_ioctl_before_grace(monkeypatch):
  """Nothing attached and we are still inside the grace period: the negotiator
  must not touch the bus. This is what protects a dock that is simply powered
  on a few seconds late -- it enumerates on its own and must not be
  power-cycled out from under itself."""
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
  assert fake_calls == ["link_up"]

  # Within CHESTNUT_LINK_RETRY_INTERVAL_S: no new calls
  negotiator.update([CHESTNUT_DUAL])
  negotiator.update([CHESTNUT_DUAL])
  assert fake_calls == ["link_up"]


def test_throttle_uses_configured_interval(monkeypatch):
  """The throttle must use CHESTNUT_LINK_RETRY_INTERVAL_S (5s), not 1s like
  the build-time compile_modeld.py loop. A 1s clock advance should still
  leave the negotiator throttled; a full interval advance should release it."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, _, _ = _make_negotiator(monkeypatch, fake_calls)
  fake_now = _freeze_clock(monkeypatch, 1000.0)

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


# ------------------------------------------------------------------ escalation
def test_failed_pokes_escalate_to_vbus_cycle(monkeypatch):
  """Pokes that do not lift the link must escalate to a VBUS power cycle (the
  software equivalent of powering the dock a few seconds after the host),
  after CHESTNUT_VBUS_CYCLE_AFTER_POKES attempts, and the poke budget must be
  refreshed so the newly re-powered dock is polled again."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, _, _ = _make_negotiator(monkeypatch, fake_calls)
  fake_now = _freeze_clock(monkeypatch, 1000.0)

  for _ in range(usb.CHESTNUT_VBUS_CYCLE_AFTER_POKES):
    negotiator.update([CHESTNUT_DUAL])
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1

  # The escalation is evaluated on the update that follows the pokes
  negotiator.update([CHESTNUT_DUAL])
  assert fake_calls == ["link_up"] * usb.CHESTNUT_VBUS_CYCLE_AFTER_POKES + ["vbus_cycle"]

  # After the cycle the poke budget is fresh again (once the throttle releases)
  fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1
  negotiator.update([CHESTNUT_DUAL])
  assert fake_calls[-1] == "link_up"
  assert fake_calls.count("vbus_cycle") == 1


def test_budget_caps_total_attempts(monkeypatch):
  """Once every ladder is spent the negotiator stops sending ioctls even
  though the device is still slow, and logs a give-up warning exactly once.

  Note: the poke count is NOT one per update -- every
  CHESTNUT_VBUS_CYCLE_AFTER_POKES pokes a cycle is spent instead and the poke
  budget restarts. Asserting "one poke per update" here was the stale
  expectation this test used to encode, which the escalation broke."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, logs, _ = _make_negotiator(monkeypatch, fake_calls)
  fake_now = _freeze_clock(monkeypatch, 1000.0)

  # Enough cycles to exhaust every ladder, but not enough wall time to trigger
  # the re-arm path.
  for _ in range(40):
    negotiator.update([CHESTNUT_DUAL])
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1

  assert fake_calls.count("vbus_cycle") == usb.CHESTNUT_VBUS_CYCLE_BUDGET
  assert len(_warnings(logs, "gave up")) == 1

  # Subsequent updates stay silent
  n_before = len(fake_calls)
  for _ in range(5):
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1
    negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == n_before


def test_link_up_failure_does_not_break_negotiator(monkeypatch):
  """If the ioctl itself raises (e.g. device briefly disappeared), the
  negotiator must swallow the exception and continue trying up to the
  budget, not abort and leave modeld hanging."""
  calls = []

  def fake_link_up():
    calls.append("link_up")
    raise OSError("device briefly gone")

  fake = types.ModuleType("openpilot.system.hardware.chestnut.flash")
  fake.link_up = fake_link_up
  fake.vbus_cycle = lambda: True
  fake.vbus_control_available = lambda: True
  monkeypatch.setitem(sys.modules, "openpilot.system.hardware.chestnut.flash", fake)

  logs = _stub_cloudlog(monkeypatch)

  negotiator = hardwared.ChestnutLinkNegotiator()
  fake_now = _freeze_clock(monkeypatch, 1000.0)

  for _ in range(3):
    negotiator.update([CHESTNUT_DUAL])
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1

  # Three link_up calls made, each raised but was caught (logged as exception)
  assert len(calls) == 3
  assert len([c for c in logs if c[0] == "exception"]) == 3
  # Did NOT hit give-up
  assert not _warnings(logs, "gave up")


def test_budget_covers_two_minute_boot_window():
  """The retry budget multiplied by the interval must cover the worst-case
  SP boot window on C3XL (~60-90s) with margin. 5s * 24 = 120s; assert this
  explicitly so a future tweak that breaks the budget gets caught here
  rather than as a C3XL-only boot regression."""
  assert usb.CHESTNUT_LINK_RETRY_INTERVAL_S * usb.CHESTNUT_LINK_RETRY_BUDGET >= 120


def test_absent_grace_covers_the_late_power_on_ordering():
  """The whole point of the grace period is not to disturb the power-on
  ordering that already works (host first, dock a few tens of seconds later),
  so it must exceed that delay."""
  assert usb.CHESTNUT_ABSENT_GRACE_S >= 30.0


def test_per_vid_pid_budget_is_independent(monkeypatch):
  """The budget is tracked per (vid, pid), not globally. A failed device
  must not block a working second chestnut plugged in later."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, logs, _ = _make_negotiator(monkeypatch, fake_calls)
  fake_now = _freeze_clock(monkeypatch, 1000.0)

  # The two devices must really be recognised as chestnuts, otherwise this
  # test silently stops exercising the per-key bookkeeping (the fixture used to
  # pair the official VID/PID with the UT3G product string, which
  # is_chestnut_runtime_device() rejects).
  official = {"vendorId": 0xADD1, "productId": 0x0001, "manufacturer": "comma",
              "product": f"custom {usb.CHESTNUT_FW_VERSION}-CLEAN", "speedMbps": 480}
  assert usb.is_chestnut_runtime_device(CHESTNUT_DUAL)
  assert usb.is_chestnut_runtime_device(official)

  # Burn every ladder on the dual VID/PID
  for _ in range(40):
    negotiator.update([CHESTNUT_DUAL])
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1
  assert len(_warnings(logs, "gave up")) == 1
  n_after_exhaust = len(fake_calls)

  # Now plug in a different chestnut device; it gets a fresh budget
  negotiator.update([official])
  assert len(fake_calls) == n_after_exhaust + 1
  assert fake_calls[-1] == "link_up"
  # The official chestnut did NOT trigger a second give-up log
  assert len(_warnings(logs, "gave up")) == 1


def test_rearm_grants_fresh_budget_after_interval(monkeypatch):
  """hardwared is long-lived: an offroad/onroad toggle does not restart it, so
  a link that burned its budget would stay dead for the whole boot ("entering
  settings mode and going onroad again does not help"). It must be re-armed a
  bounded number of times instead."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, logs, _ = _make_negotiator(monkeypatch, fake_calls)
  fake_now = _freeze_clock(monkeypatch, 1000.0)

  for _ in range(40):
    negotiator.update([CHESTNUT_DUAL])
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1
  assert len(_warnings(logs, "gave up")) == 1
  n_before = len(fake_calls)

  # Inside the re-arm window: still quiet
  fake_now[0] += 1.0
  negotiator.update([CHESTNUT_DUAL])
  assert len(fake_calls) == n_before

  # Past the re-arm interval: poking resumes
  fake_now[0] += usb.CHESTNUT_REARM_INTERVAL_S
  negotiator.update([CHESTNUT_DUAL])
  assert len(_warnings(logs, "re-armed")) == 1
  assert fake_calls[-1] == "link_up"

  # ...but only CHESTNUT_REARM_BUDGET times per boot
  for _ in range(600):
    fake_now[0] += usb.CHESTNUT_LINK_RETRY_INTERVAL_S + 0.1
    negotiator.update([CHESTNUT_DUAL])
  assert len(_warnings(logs, "re-armed")) == usb.CHESTNUT_REARM_BUDGET


# ----------------------------------------------------------- absent dock ladder
def test_absent_dock_not_touched_during_grace(monkeypatch):
  """The grace period must outlast a dock that is merely powered on a few
  seconds late, otherwise the recovery would power-cycle the very dock that is
  coming up correctly."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, _, fake_now = _make_negotiator_frozen(monkeypatch)

  fake_now[0] += usb.CHESTNUT_ABSENT_GRACE_S - 1.0
  negotiator.update([])
  assert fake_calls == []


def test_absent_dock_cycles_vbus_after_grace(monkeypatch):
  """The simultaneous-power-on failure mode: the dock never enumerates, so
  there is nothing to poke. VBUS must be cycled blind once the grace period
  expires, and the reason must be logged."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, logs, fake_now = _make_negotiator_frozen(monkeypatch)

  fake_now[0] += usb.CHESTNUT_ABSENT_GRACE_S + 1.0
  negotiator.update([])
  assert fake_calls == ["vbus_cycle"]
  assert len(_warnings(logs, "no chestnut eGPU enumerated")) == 1


def test_absent_dock_cycles_are_throttled_and_bounded(monkeypatch):
  """Repeated blind cycles are both rate-limited and capped per boot so a
  genuinely broken dock is not thrashed."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, _, fake_now = _make_negotiator_frozen(monkeypatch)

  fake_now[0] += usb.CHESTNUT_ABSENT_GRACE_S + 1.0
  negotiator.update([])
  assert fake_calls == ["vbus_cycle"]

  # Within the interval: no second cycle
  fake_now[0] += usb.CHESTNUT_ABSENT_VBUS_CYCLE_INTERVAL_S - 1.0
  negotiator.update([])
  assert fake_calls == ["vbus_cycle"]

  # After the interval: one more, then bounded
  for _ in range(20):
    fake_now[0] += usb.CHESTNUT_ABSENT_VBUS_CYCLE_INTERVAL_S + 1.0
    negotiator.update([])
  assert fake_calls.count("vbus_cycle") == usb.CHESTNUT_ABSENT_VBUS_CYCLE_BUDGET


def test_healthy_link_releases_absent_budget(monkeypatch):
  """A dock that comes up (late, or after a cycle) must release the ladder, so
  a later drop-out gets a full recovery budget again."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, _, fake_now = _make_negotiator_frozen(monkeypatch)

  fake_now[0] += usb.CHESTNUT_ABSENT_GRACE_S + 1.0
  negotiator.update([])
  assert fake_calls == ["vbus_cycle"]

  # Dock comes up
  negotiator.update([{**CHESTNUT_DUAL, "speedMbps": 5000}])
  assert fake_calls == ["vbus_cycle"]

  # ...and drops out again later: the ladder starts over
  fake_now[0] += usb.CHESTNUT_ABSENT_VBUS_CYCLE_INTERVAL_S + 1.0
  negotiator.update([])
  assert fake_calls == ["vbus_cycle", "vbus_cycle"]


def test_absent_recovery_respects_total_vbus_budget(monkeypatch):
  """The absent ladder and the escalation ladder share a per-boot VBUS budget."""
  fake_calls = _install_fake_flash(monkeypatch)
  negotiator, _, fake_now = _make_negotiator_frozen(monkeypatch)
  negotiator._vbus_cycles_total = usb.CHESTNUT_VBUS_CYCLE_BUDGET_TOTAL

  fake_now[0] += usb.CHESTNUT_ABSENT_GRACE_S + 1.0
  negotiator.update([])
  assert fake_calls == []


def test_absent_dock_without_vbus_control_warns_once(monkeypatch):
  """On a unit without smb2-vbus control there is nothing to escalate to; say so
  once rather than silently doing nothing."""
  fake_calls = _install_fake_flash(monkeypatch, vbus_control=False)
  negotiator, logs, fake_now = _make_negotiator_frozen(monkeypatch)

  fake_now[0] += usb.CHESTNUT_ABSENT_GRACE_S + 1.0
  for _ in range(5):
    negotiator.update([])
    fake_now[0] += usb.CHESTNUT_ABSENT_VBUS_CYCLE_INTERVAL_S + 1.0

  assert fake_calls == []
  assert len(_warnings(logs, "no smb2-vbus control")) == 1


def test_vbus_cycle_that_failed_is_logged_honestly(monkeypatch):
  """flash.vbus_cycle() now reports whether the rail was really toggled. A
  failed toggle must be visible in the log instead of being reported as a
  recovery attempt that never happened."""
  fake_calls = _install_fake_flash(monkeypatch, vbus_control=True, vbus_succeeds=False)
  negotiator, logs, fake_now = _make_negotiator_frozen(monkeypatch)

  fake_now[0] += usb.CHESTNUT_ABSENT_GRACE_S + 1.0
  negotiator.update([])
  assert fake_calls == []
  assert len(_warnings(logs, "did nothing")) == 1
