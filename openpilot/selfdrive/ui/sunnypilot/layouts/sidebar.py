"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

SP8516 定制：SUNNYLINK 状态卡替换为「模型状态卡」（小模型 / 加载进度 / 功耗温度），
见 selfdrive/ui/egpu_status.py。原 sunnylink 逻辑移除。
"""
import math
import time

import pyray as rl
from dataclasses import dataclass
from openpilot.selfdrive.ui.ui_state import ui_state, ChestnutState
from openpilot.selfdrive.ui.egpu_status import build_model_sidebar_status
from openpilot.system.ui.lib.application import gui_app


METRIC_HEIGHT = 126
METRIC_MARGIN = 30
METRIC_START_Y = 300
HOME_BTN = rl.Rectangle(60, 860, 180, 180)

CHESTNUT_ICON_WIDTH = 180
CHESTNUT_ICON_HEIGHT = 133


# Color scheme
class Colors:
  WHITE = rl.WHITE
  WHITE_DIM = rl.Color(255, 255, 255, 85)
  GRAY = rl.Color(84, 84, 84, 255)

  # Status colors
  GOOD = rl.WHITE
  WARNING = rl.Color(218, 202, 37, 255)
  DANGER = rl.Color(201, 34, 49, 255)
  PROGRESS = rl.Color(0, 134, 233, 255)
  DISABLED = rl.Color(128, 128, 128, 255)

  # UI elements
  METRIC_BORDER = rl.Color(255, 255, 255, 85)
  BUTTON_NORMAL = rl.WHITE
  BUTTON_PRESSED = rl.Color(255, 255, 255, 166)


@dataclass(slots=True)
class MetricData:
  label: str
  value: str
  color: rl.Color

  def update(self, label: str, value: str, color: rl.Color):
    self.label = label
    self.value = value
    self.color = color


class SidebarSP:
  def __init__(self):
    self._model_status = MetricData("模型", "小模型", Colors.DISABLED)
    self._loading_since: float | None = None
    self._chestnut_green_img = gui_app.texture("icons_mici/chestnut_green.png", CHESTNUT_ICON_WIDTH, CHESTNUT_ICON_HEIGHT)
    self._chestnut_default_img = gui_app.texture("icons_mici/chestnut.png", CHESTNUT_ICON_WIDTH, CHESTNUT_ICON_HEIGHT)
    self._chestnut_orange_img = gui_app.texture("icons_mici/chestnut_orange.png", CHESTNUT_ICON_WIDTH, CHESTNUT_ICON_HEIGHT)
    self._egpu_offline_img = gui_app.texture("icons_mici/egpu_offline.png", CHESTNUT_ICON_WIDTH, CHESTNUT_ICON_HEIGHT)

  def _update_model_status(self):
    state = ui_state.chestnut_state
    # chestnutState 由 hardwared 的 chestnut_state_thread 发布（GPU SMU 遥测）；
    # recv_frame == 0 表示还没收到过，字段是 capnp 默认 0，不要拿来显示。
    have_telemetry = ui_state.sm.recv_frame.get("chestnutState", 0) > 0
    telemetry = ui_state.sm["chestnutState"]
    temp_c = float(telemetry.tempC) if have_telemetry else 0.0
    power_w = float(telemetry.powerDrawW) if have_telemetry else 0.0
    gpu_usage = int(telemetry.gpuUsagePercent) if have_telemetry else 0

    # 加载进度：进入 LOADING 记录起点，40s 时长估算（99% 封顶）；离开清零
    if state == ChestnutState.LOADING:
      if self._loading_since is None:
        self._loading_since = time.monotonic()
      elapsed = time.monotonic() - self._loading_since
    else:
      self._loading_since = None
      elapsed = None

    label, value, severity, _detail = build_model_sidebar_status(
      state_name=state.value, temp_c=temp_c, power_w=power_w,
      gpu_usage=gpu_usage, loading_elapsed_s=elapsed)
    color = {
      "good": Colors.GOOD,
      "warning": Colors.WARNING,
      "danger": Colors.DANGER,
      "progress": Colors.PROGRESS,
      "disabled": Colors.DISABLED,
    }.get(severity, Colors.DISABLED)
    self._model_status.update(label, value, color)

  def _get_home_icon(self, default_img: rl.Texture) -> tuple[rl.Texture, rl.Vector2, float]:
    state = ui_state.chestnut_state
    if state == ChestnutState.DISCONNECTED:
      icon = self._egpu_offline_img
      x = HOME_BTN.x + (HOME_BTN.width - icon.width) / 2
      y = HOME_BTN.y + (HOME_BTN.height - icon.height) / 2
      return icon, rl.Vector2(x, y), 1.0

    if state == ChestnutState.LOADING:
      icon = self._chestnut_default_img
      opacity = 0.35 + 0.65 * (0.5 - 0.5 * math.cos(rl.get_time() * 6.0))
    elif state in (ChestnutState.UNCOMPILED, ChestnutState.FAILED):
      icon, opacity = self._chestnut_orange_img, 1.0
    else:
      icon, opacity = self._chestnut_green_img, 1.0

    x = HOME_BTN.x + (HOME_BTN.width - icon.width) / 2
    y = HOME_BTN.y + (HOME_BTN.height - icon.height) / 2
    return icon, rl.Vector2(x, y), opacity

  def _draw_metrics_w_model(self, rect: rl.Rectangle, _temp, _panda, _connect):
    metrics = [_temp, _panda, _connect, self._model_status]
    start_y = int(rect.y) + METRIC_START_Y
    available_height = max(0, int(HOME_BTN.y) - METRIC_MARGIN - METRIC_HEIGHT - start_y)
    spacing = available_height / max(1, len(metrics) - 1)

    return metrics, start_y, spacing
