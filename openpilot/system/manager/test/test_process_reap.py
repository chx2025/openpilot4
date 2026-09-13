#!/usr/bin/env python3
"""Tests for the manager's reap/relaunch path.

modeld_tinygrad deliberately exits so manager relaunches it (see
sunnypilot/modeld_v2/modeld.py's chestnut self-heal). These tests pin the
contract that makes that work, and guard the stock behaviour for every other
process.
"""
import os
import time
import unittest
from unittest import mock

from openpilot.common.params import Params
from openpilot.common.test import OpenpilotTestCase
from openpilot.system.manager.process import NativeProcess, PythonProcess, ensure_running
from openpilot.system.manager.process_config import managed_processes


def _always_run(started, params, CP):
  return True


class FakeProcess:
  """Minimal stand-in for multiprocessing.Process for reap tests."""
  _next_pid = 8000

  def __init__(self, name=None, target=None, args=()):
    FakeProcess._next_pid += 1
    self.pid = FakeProcess._next_pid
    self.name = name
    self.exitcode = None

  def start(self):
    pass

  def is_alive(self):
    return self.exitcode is None

  def join(self, timeout=None):
    pass


def _fake_kill(pid, sig):
  """Make the fakes exit instead of signalling a real (nonexistent) pid."""
  for p in FakeProcess._instances:
    if p.pid == pid:
      p.exitcode = 0 if sig == 0 else -int(sig)


FakeProcess._instances = []
_orig_init = FakeProcess.__init__


def _init(self, name=None, target=None, args=()):
  _orig_init(self, name, target, args)
  FakeProcess._instances.append(self)


FakeProcess.__init__ = _init


def _mk_proc(name, should_run=None, **kwargs):
  p = PythonProcess(name, "some.module", should_run or _always_run, **kwargs)
  p.proc = FakeProcess(name)
  return p


class TestProcessReap(OpenpilotTestCase):
  def setUp(self):
    self.params = Params()
    patchers = [
      mock.patch("openpilot.system.manager.process.Params", return_value=self.params),
      mock.patch("openpilot.system.manager.process.Process", FakeProcess),
      mock.patch("openpilot.system.manager.process.os.kill", _fake_kill),
    ]
    for pt in patchers:
      self.addCleanup(pt.stop)
      pt.start()

  def test_reap_disabled_by_default(self):
    """Stock processes must not be resurrected."""
    assert PythonProcess("x", "m", _always_run).reap is False
    assert NativeProcess("x", "cwd", ["cmd"], _always_run).reap is False

  def test_default_process_stays_dead(self):
    p = _mk_proc("stock")
    p.proc.exitcode = 0
    for _ in range(5):
      ensure_running([p], True, self.params, None)
    assert p.proc is not None
    assert p.proc.exitcode == 0, "a non-reaped process must stay dead"

  def test_reaped_process_is_relaunched(self):
    p = _mk_proc("egpu", reap=True, restart_delay_s=0.0)
    first_pid = p.proc.pid
    p.proc.exitcode = 0
    ensure_running([p], True, self.params, None)
    assert p.proc is not None
    assert p.proc.pid != first_pid, "a reaped process must be given a new child"
    assert p.proc.is_alive()

  def test_repeated_same_exit_code_is_relaunched_each_time(self):
    """Reporting must key on the child instance, not the exit code."""
    p = _mk_proc("repeat", reap=True, restart_delay_s=0.0)
    seen = []
    for _ in range(3):
      p.proc.exitcode = 0
      ensure_running([p], True, self.params, None)
      seen.append(p.proc.pid)
    assert len(set(seen)) == 3, f"expected 3 distinct children, got {seen}"

  def test_restart_delay_throttles_repeats(self):
    p = _mk_proc("slow", reap=True, restart_delay_s=60.0)
    p.proc.exitcode = 0
    # the first exit is handled immediately
    assert p.should_reap(time.monotonic())
    ensure_running([p], True, self.params, None)
    p.proc.exitcode = 0
    # a second exit inside the cooldown must wait
    assert not p.should_reap(time.monotonic())
    p._restart_deadline = time.monotonic() - 1.0
    assert p.should_reap(time.monotonic())

  def test_restart_budget_is_enforced(self):
    p = _mk_proc("budgeted", reap=True, restart_delay_s=0.0, restart_budget=2)
    # Kill the child repeatedly. The first two deaths are within budget and get
    # relaunched; after that the process is dropped and must stay dropped.
    for i in range(5):
      if p.proc is not None:
        p.proc.exitcode = 1
      ensure_running([p], True, self.params, None)
      if i < 2:
        assert p.proc is not None and p.proc.is_alive(), f"iteration {i} should still be within budget"
    assert p.proc is None, "after the budget is spent the process must be dropped"
    assert p.enabled is False, "a budget-exhausted process must not be respawned"

    # and it must never come back on later ticks
    for _ in range(3):
      ensure_running([p], True, self.params, None)
    assert p.proc is None, "a budget-exhausted process must stay dropped"

  def test_should_run_false_does_not_relaunch(self):
    # The process is only "supposed to run" onroad; when offroad the manager
    # must stop it and never relaunch it, regardless of the reap flag.
    p = _mk_proc("offroad", should_run=lambda started, params, CP: bool(started),
                 reap=True, restart_delay_s=0.0)
    for _ in range(3):
      ensure_running([p], False, self.params, None)
    assert p.proc is None or not p.proc.is_alive(), "a stopped process must not run"

    # going back onroad lets it start again
    ensure_running([p], True, self.params, None)
    assert p.proc is not None and p.proc.is_alive()

  def test_healthy_process_is_untouched(self):
    p = _mk_proc("healthy", reap=True, restart_delay_s=0.0)
    pid = p.proc.pid
    for _ in range(5):
      ensure_running([p], True, self.params, None)
    assert p.proc.pid == pid

  def test_native_process_supports_reap(self):
    p = NativeProcess("modeld_tinygrad", "openpilot/sunnypilot/modeld_v2", ["./modeld"],
                      _always_run, reap=True, restart_delay_s=0.0)
    p.proc = FakeProcess("modeld_tinygrad")
    first = p.proc.pid
    p.proc.exitcode = 0
    ensure_running([p], True, self.params, None)
    assert p.proc.pid != first


class TestReapWiring(OpenpilotTestCase):
  def test_only_modeld_tinygrad_is_reaped(self):
    """The reap path exists for modeld's self-heal; nothing else may opt in
    without a deliberate decision, since it changes crash semantics."""
    reaped = [p.name for p in managed_processes.values() if getattr(p, "reap", False)]
    assert reaped == ["modeld_tinygrad"], f"unexpected reap processes: {reaped}"

  def test_modeld_tinygrad_has_a_restart_delay(self):
    p = managed_processes["modeld_tinygrad"]
    assert p.restart_delay_s > 0.0, "a restart delay avoids tight relaunch loops"


if __name__ == "__main__":
  unittest.main()
