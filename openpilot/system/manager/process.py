import importlib
import os
import signal
import time
import subprocess
from collections.abc import Callable, ValuesView
from abc import ABC, abstractmethod
from multiprocessing import Process

from setproctitle import setproctitle

from openpilot.cereal import log
from opendbc.car.structs import car
import openpilot.cereal.messaging as messaging
import openpilot.system.sentry as sentry
from openpilot.common.basedir import BASEDIR
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog


def launcher(proc: str, name: str) -> None:
  try:
    # import the process
    mod = importlib.import_module(proc)

    # rename the process
    setproctitle(proc)

    # create new context since we forked
    messaging.reset_context()

    # add daemon name tag to logs
    cloudlog.bind(daemon=name)
    sentry.set_tag("daemon", name)

    # exec the process
    mod.main()
  except KeyboardInterrupt:
    cloudlog.warning(f"child {proc} got SIGINT")
  except Exception:
    # can't install the crash handler because sys.excepthook doesn't play nice
    # with threads, so catch it here.
    sentry.capture_exception()
    raise


def nativelauncher(pargs: list[str], cwd: str, name: str) -> None:
  os.environ['MANAGER_DAEMON'] = name

  # exec the process
  os.chdir(cwd)
  os.execvp(pargs[0], pargs)


def join_process(process: Process, timeout: float) -> None:
  # Process().join(timeout) will hang due to a python 3 bug: https://bugs.python.org/issue28382
  # We have to poll the exitcode instead
  t = time.monotonic()
  while time.monotonic() - t < timeout and process.exitcode is None:
    time.sleep(0.001)


class ManagerProcess(ABC):
  daemon = False
  sigkill = False
  should_run: Callable[[bool, Params, car.CarParams], bool]
  proc: Process | None = None
  enabled = True
  name = ""
  shutting_down = False
  # reap=True makes ensure_running() relaunch this process if it exits on its own
  # while it is still supposed to be running. Off by default to preserve stock
  # semantics; opt in only where a process deliberately exits to re-initialize
  # (e.g. modeld_tinygrad re-evaluating the eGPU at startup).
  reap = False
  restart_delay_s = 5.0
  restart_budget = 0            # 0 = unlimited
  restart_window_s = 0.0        # 0 = count for the lifetime of the manager

  def __init__(self) -> None:
    self._reap_reported_pid: int | None = None
    self._restart_deadline = 0.0
    self._restart_count = 0
    self._restart_first_t = 0.0

  def dead(self) -> bool:
    """True when a launched child has exited on its own and has not been reaped yet."""
    return self.proc is not None and self.proc.exitcode is not None

  def should_reap(self, now: float) -> bool:
    """True when a reaped process is due to be reported/relaunched.

    The first exit is handled immediately; restart_delay_s then throttles how
    soon a *subsequent* exit may be reaped, so a crash-looping child cannot be
    relaunched on every single manager tick.
    """
    return bool(self.reap and self.dead() and now >= self._restart_deadline)

  def reap_dead_child(self, now: float) -> None:
    """Report and (if allowed) relaunch a child that exited on its own.

    No-op unless reap=True, so the default manager behaviour is unchanged.
    """
    if not self.reap or self.proc is None:
      return
    exit_code = self.proc.exitcode
    if exit_code is None:
      return
    # Identify the exit by pid, not by exit code: a relaunched child that dies
    # with the same code must still be reported (else it would never be
    # restarted again).
    pid = self.proc.pid
    if pid is not None and pid == self._reap_reported_pid:
      return
    self._reap_reported_pid = pid

    if not self._restart_allowed(now):
      cloudlog.warning(f"process {self.name} exited with {exit_code}, not restarting")
      self.proc = None
      # Terminal state: ensure_running() must not relaunch it again, otherwise
      # the process would be spawned once per manager tick and immediately
      # abandoned.
      self.enabled = False
      return

    cloudlog.warning(f"process {self.name} exited with {exit_code}, restarting")
    self.proc = None
    if self.restart_delay_s > 0.0:
      self._restart_deadline = now + max(0.0, self.restart_delay_s)

  def _restart_allowed(self, now: float) -> bool:
    if self.restart_budget <= 0:
      return True
    if self.restart_window_s > 0.0 and now - self._restart_first_t > self.restart_window_s:
      self._restart_count = 0
      self._restart_first_t = now
    if self.restart_budget > 0 and self._restart_count >= self.restart_budget:
      cloudlog.warning(f"process {self.name} restart budget exhausted ({self._restart_count}), giving up")
      return False
    self._restart_count += 1
    if self._restart_count == 1:
      self._restart_first_t = now
    return True

  @abstractmethod
  def start(self) -> None:
    pass

  def stop(self, retry: bool = True, block: bool = True, sig: signal.Signals | None = None) -> int | None:
    if self.proc is None:
      return None

    if self.proc.exitcode is None:
      if not self.shutting_down:
        cloudlog.info(f"killing {self.name}")
        if sig is None:
          sig = signal.SIGKILL if self.sigkill else signal.SIGINT
        self.signal(sig)
        self.shutting_down = True

        if not block:
          return None

      join_process(self.proc, 5)

      # If process failed to die send SIGKILL
      if self.proc.exitcode is None and retry:
        cloudlog.info(f"killing {self.name} with SIGKILL")
        self.signal(signal.SIGKILL)
        self.proc.join()

    ret = self.proc.exitcode
    cloudlog.info(f"{self.name} is dead with {ret}")

    if self.proc.exitcode is not None:
      self.shutting_down = False
      self.proc = None

    return ret

  def signal(self, sig: int) -> None:
    if self.proc is None:
      return

    # Don't signal if already exited
    if self.proc.exitcode is not None and self.proc.pid is not None:
      return

    # Can't signal if we don't have a pid
    if self.proc.pid is None:
      return

    cloudlog.info(f"sending signal {sig} to {self.name}")
    os.kill(self.proc.pid, sig)

  def get_process_state_msg(self):
    state = log.ManagerState.ProcessState.new_message()
    state.name = self.name
    if self.proc:
      state.running = self.proc.is_alive()
      state.shouldBeRunning = self.proc is not None and not self.shutting_down
      state.pid = self.proc.pid or 0
      state.exitCode = self.proc.exitcode or 0
    return state


class NativeProcess(ManagerProcess):
  def __init__(self, name, cwd, cmdline, should_run, enabled=True, sigkill=False,
               reap=False, restart_delay_s=5.0, restart_budget=0, restart_window_s=0.0):
    super().__init__()
    self.name = name
    self.cwd = cwd
    self.cmdline = cmdline
    self.should_run = should_run
    self.enabled = enabled
    self.sigkill = sigkill
    self.reap = reap
    self.restart_delay_s = restart_delay_s
    self.restart_budget = restart_budget
    self.restart_window_s = restart_window_s
    self.launcher = nativelauncher

  def start(self) -> None:
    # In case we only tried a non blocking stop we need to stop it before restarting
    if self.shutting_down:
      self.stop()

    if self.proc is not None:
      return

    cwd = os.path.join(BASEDIR, self.cwd)
    cloudlog.info(f"starting process {self.name}")
    self.proc = Process(name=self.name, target=self.launcher, args=(self.cmdline, cwd, self.name))
    self.proc.start()
    self.shutting_down = False


class PythonProcess(ManagerProcess):
  def __init__(self, name, module, should_run, enabled=True, sigkill=False,
               reap=False, restart_delay_s=5.0, restart_budget=0, restart_window_s=0.0):
    super().__init__()
    self.name = name
    self.module = module
    self.should_run = should_run
    self.enabled = enabled
    self.sigkill = sigkill
    self.reap = reap
    self.restart_delay_s = restart_delay_s
    self.restart_budget = restart_budget
    self.restart_window_s = restart_window_s
    self.launcher = launcher

  def start(self) -> None:
    # In case we only tried a non blocking stop we need to stop it before restarting
    if self.shutting_down:
      self.stop()

    if self.proc is not None:
      return

    cloudlog.info(f"starting python {self.module}")
    self.proc = Process(name=self.name, target=self.launcher, args=(self.module, self.name))
    self.proc.start()
    self.shutting_down = False


class DaemonProcess(ManagerProcess):
  """Python process that has to stay running across manager restart.
  This is used for athena so you don't lose SSH access when restarting manager."""
  def __init__(self, name, module, param_name, enabled=True):
    super().__init__()
    self.name = name
    self.module = module
    self.param_name = param_name
    self.enabled = enabled
    self.params = None

  @staticmethod
  def should_run(started, params, CP):
    return True

  def start(self) -> None:
    if self.params is None:
      self.params = Params()

    pid = self.params.get(self.param_name)
    if pid is not None:
      try:
        os.kill(int(pid), 0)
        with open(f'/proc/{pid}/cmdline') as f:
          if self.module in f.read():
            # daemon is running
            return
      except (OSError, FileNotFoundError):
        # process is dead
        pass

    cloudlog.info(f"starting daemon {self.name}")
    proc = subprocess.Popen(['python', '-m', self.module],
                               stdin=open('/dev/null'),
                               stdout=open('/dev/null', 'w'),
                               stderr=open('/dev/null', 'w'),
                               preexec_fn=os.setpgrp)

    self.params.put(self.param_name, proc.pid, block=True)

  def stop(self, retry=True, block=True, sig=None) -> None:
    pass


def ensure_running(procs: ValuesView[ManagerProcess], started: bool, params: Params, CP: car.CarParams,
                   not_run: list[str] | None=None) -> list[ManagerProcess]:
  if not_run is None:
    not_run = []

  now = time.monotonic()
  running = []
  for p in procs:
    if p.enabled and p.name not in not_run and p.should_run(started, params, CP):
      running.append(p)
    else:
      p.stop(block=False)

  # Relaunch opted-in processes whose child exited on its own while it is still
  # supposed to be running. start() would otherwise no-op because self.proc is
  # still set, and the process would stay dead until an offroad/onroad toggle.
  for p in running:
    if p.should_reap(now):
      p.reap_dead_child(now)

  for p in running:
    p.start()

  return running
