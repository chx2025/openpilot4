"""gas_long_cancel.py — 踩油门挂起纵向 / 松油门立即恢复纵向（2026-09-28）

需求原文（用户 2026-09-28）
──────────────────────────────────────────────────────────────────────────
 1. **踩油门 ⇒ 取消纵向**（不是"把减速减小"，是把整层纵向挂起）。
 2. **松开油门 ⇒ 立即恢复纵向**（当帧接管，无空窗期）。
 3. 如果纵向是被**刹车**或**按键**取消掉的，那么之后再怎么踩油门 / 松油门
    **都不能**把纵向恢复回来 —— 「踩油门挂起」和「真的取消」必须分清楚。
 4. 用户原话「要不你给我搞个逻辑，让我试一试」⇒ 必须先做到：
    * **单变量**：只动纵向；横向（转向 / CC.latActive）一个字都不碰。
    * **一键回退**：运行期一个参数 `GasLongCancel` 就能开关，不用重刷代码。
    * 不改上游候选池 / MPC / LongControl / 任何让位模块。


一句话实现
──────────────────────────────────────────────────────────────────────────
不改 planner，也不改 LongControl。只在 controlsd 算完纵向总闸之后，把
`CC.longActive` 再与一个「挂起」标志相与：

    CC.longActive = CC.enabled and (...) and not gas_long_cancel.suspended

`longActive=False` 在本 fork 里就是**标准的"纵向挂起"通道**，而且链路
每一步都已实证（2026-09-28 逐条读码确认）：

    controlsd.py:132   if not CC.longActive: self.LoC.reset()      # 纵向 PID 积分器清零
    longcontrol.py:83  if long_control_state == off: reset(); output_accel = 0.
                       ⇒ actuators.accel 恒为 0.0（不是"减小"，是真正的 0）
    toyota/carcontroller.py:216,240,258
                       longActive 为假 ⇒ pcm_accel_cmd = actuators.accel = 0，
                       并执行 self.long_pid.reset()
    toyota/carcontroller.py:272
                       pcm_cancel_cmd **不受 longActive 影响**（它由
                       CC.cruiseControl.cancel 独立驱动，只在 disengage 时置位）
                       ⇒ 挂起**不会取消车的 ACC**，不需要按键就能回来

即：挂起期间 PCM 只做滑行，纵向上「谁都不管」；松油门当帧 longActive 回真，
纵向 PID 从零重新接管（挂起期间的累积量已经被 reset 掉）。


需求 3（"分清楚是谁取消的"）为什么天然成立、不需要额外判断
──────────────────────────────────────────────────────────────────────────
**只有「踩下油门那一帧 openpilot 正处于 engaged」才能武装（arm）。**

  * 刹车取消 / 按键取消 ⇒ `selfdriveState.enabled=False`
    （selfdrived 的取消事件走 `ET.USER_DISABLE` ⇒ `state.py:31-33` 置
     `State.disabled`），而 controlsd 的 `CC.enabled = sm['selfdriveState'].enabled`。
  * 于是本模块在 `engaged=False` 时**立即解除武装**，并且此后
    「踩油门/松油门」无论如何都不进入挂起分支；
    纵向能不能回来只由 `CC.enabled` 决定 —— 也就是必须靠用户**重新 engage**
    （RESUME 键 / 油门-刹车逻辑），这正是用户要的语义。
  * 反过来，「重新 engage 之后纵向正常工作」也不受影响：武装只看油门**上升沿**，
    所以 "按键重新 engage 时脚还压在油门上" 不会把纵向锁死。

★ 例外/已知取舍（明确写出来，免得日后当 bug 查）
  * **只在油门上升沿武装**。若「engage 的那一刻脚已经压在油门上」，
    不武装 ⇒ 纵向立刻正常接管。此时踩油门的减速度抑制由既有模块
    `gas_override`（GAS_PRESSED_A_FLOOR=0.0，patch11）兜住，不会打架。
    想改成"engage 时脚在油门上也算武装"，只需在 `update()` 里加一个
    `prev_engaged` 条件（一处，已注释标出位置）。
  * ★ `update()` **每帧都会被调用，包括未 engaged 时**，所以 `prev_gas` 始终
    是真实踏板状态：在未 engaged 期间踩下的油门**不会被误判成上升沿**。
    （这条是被单测抓出来的 —— 第一版用例没先喂未-engaged 帧，
     把模块的正确行为误判成了 bug。别再删这个行为。）
  * 参数 `GasLongCancel` 在"油门已经踩着"的当口被拨到 1 时，本帧不武装
    （没有上升沿）；松脚再踩一次即生效。属预期。
  * 挂起期间 `CC.cruiseControl.override` 会自动变真（controlsd.py:181 由
    `not CC.longActive` 推导）。全仓消费者只有两家：
    hyundai/volkswagen 的车控（本车是丰田，无关）与 UI 里设定速度那格的颜色
    （ui/sunnypilot/onroad/hud_renderer.py:83,101，纯装饰，**不响铃**）。
    ⇒ 表现为 HUD 上"纵向被接管"的配色提示，符合语义。


参数（声明在 common/params_keys.h）
──────────────────────────────────────────────────────────────────────────
  GasLongCancel            BOOL  默认 "1"（dp 2026-10-20 改为默认开启）。1 = 开启本功能。
  GasLongCancelResumeMs    INT   默认 "200"（dp 2026-10-20：松油门 0.2 s 后恢复）。
                                 松油门后恢复纵向的等待时间（ms）；0 = 当帧立即接管。

⚠️ `Params.get_bool()` 对**未设置**的键返回 False 而**不走 default_value 回退**
  （gas_override.py 里已经踩过这个坑），所以本模块统一用
  `get(key, return_default=True)` 取声明里的默认值。
"""

import time

from openpilot.common.realtime import DT_CTRL

# ==== 开关 ==================================================================
# 代码级总开关（发版兜底）。与参数 GasLongCancel 取 AND：任一为关 ⇒ 本模块透明。
GAS_LONG_CANCEL_ENABLE: bool = True
# 运行期开关（BOOL，默认 "1" = 开；dp 2026-10-20 由 "0" 改为默认开启）。声明见 common/params_keys.h。
GAS_LONG_CANCEL_PARAM_KEY: str = "GasLongCancel"
# 恢复延时（INT，单位 ms）。默认 "200" = 松油门 0.2 s 后接管（dp 2026-10-20 需求）。声明见 common/params_keys.h。
GAS_LONG_CANCEL_RESUME_PARAM_KEY: str = "GasLongCancelResumeMs"
# 参数轮询周期（s）：1 Hz，拨开关后约 1 s 内生效，**不需要重启**。
GAS_LONG_CANCEL_PARAMS_PERIOD_S: float = 1.0

# ==== 数值 ==================================================================
# 松油门后恢复纵向的等待时间（秒）。★ 这里只是「参数读不到时的兜底值 + 首帧种子」；
# 实际生效值来自 GasLongCancelResumeMs（声明里默认 "200" ⇒ 0.2 s）。
GAS_LONG_CANCEL_RESUME_S: float = 0.2
# 参数允许范围（夹取，防止手写参数把控制流卡死）
GAS_LONG_CANCEL_RESUME_S_MIN: float = 0.0
GAS_LONG_CANCEL_RESUME_S_MAX: float = 5.0

# ==== 诊断 ==================================================================
# 探针开关（关掉后模块行为逐位不变，只是不打日志）。
GAS_LONG_CANCEL_DIAG: bool = True
# 挂起期间的 1 Hz 心跳周期（s）；<= 0 表示只打相位跳变、不打心跳。
GAS_LONG_CANCEL_HEARTBEAT_S: float = 1.0

# ==== 相位（探针用；也是本模块唯一的状态描述）==============================
PHASE_OFF = "off"        # 未开启：代码级关闭 / 参数为 0 / engaged=False（含被刹车·按键取消）
PHASE_READY = "ready"    # 已开启且 engaged，但没在挂起 —— 纵向正常交给原逻辑
PHASE_HELD = "held"      # 正踩着油门 ⇒ 挂起中（longActive 被压掉）
PHASE_WAIT = "wait"      # 已松油门，恢复倒计时中 ⇒ 仍挂起


class GasLongCancel:
  """踩油门挂起纵向 / 松油门延时恢复。

  用法（controlsd.state_control 内）：

      if self.gas_long_cancel.update(bool(CS.gasPressed), CC.enabled, CS.vEgo, CS.aEgo):
        CC.longActive = False

  必须放在 `CC.longActive = ...` 之后、`if not CC.longActive: self.LoC.reset()` 之前，
  这样挂起与恢复都会顺带执行 LoC.reset()。
  """

  def __init__(self, params=None, enabled: bool | None = None):
    self._params = params
    self.enabled = GAS_LONG_CANCEL_ENABLE if enabled is None else bool(enabled)
    self.resume_frames = self._frames_from_s(GAS_LONG_CANCEL_RESUME_S)
    self._param_t = -GAS_LONG_CANCEL_PARAMS_PERIOD_S   # 第一帧就立刻读一次
    self._param_note = None                            # 参数变化待播报（延迟到下次输出）
    self._log_next_t = 0.0
    self._last_sig = None
    self._frame = 0

    # ---- 状态 ----
    self.armed = False        # 本次挂起是否由「踩油门」发起（被 true-cancel 打断则清掉）
    self.timer = 0            # 松油门后的恢复倒计时（帧）
    self.prev_gas = False
    self.suspended = False    # 对外输出：True ⇒ 应当把 longActive 压成 False
    self.phase = PHASE_OFF

    # ---- 诊断计数（跨会话累计，便于日志里一眼看出"踩了多少脚"）----
    self.n_suspend = 0   # 武装次数（= 踩下油门的次数）
    self.n_resume = 0    # 自动恢复次数
    self.n_disarm = 0    # 因 engaged 掉线而解除武装的次数（刹车/按键取消）

  # --------------------------------------------------------------------------
  @staticmethod
  def _frames_from_s(seconds: float) -> int:
    seconds = min(max(float(seconds), GAS_LONG_CANCEL_RESUME_S_MIN), GAS_LONG_CANCEL_RESUME_S_MAX)
    return int(round(seconds / DT_CTRL))

  @staticmethod
  def _to_float(value, fallback: float) -> float:
    try:
      if isinstance(value, bytes):
        value = value.decode()
      return float(value)
    except (TypeError, ValueError):
      return fallback

  @staticmethod
  def _as_bool(value) -> bool:
    """★ 必须显式解析：Params.get() 返回的是 '0'/'1' 字符串（或 bytes），
    而 bool('0') 是 True —— 直接 bool() 会让开关永远关不掉。"""
    if isinstance(value, bytes):
      value = value.decode()
    if isinstance(value, bool):
      return value
    return str(value).strip().lower() not in ('', '0', 'false', 'off', 'none')

  def _refresh_params(self, now: float) -> None:
    """1 Hz 热刷新两个参数。任何异常都只影响"能不能开关"，绝不影响纵向控制。"""
    if self._params is None:
      return
    if now - self._param_t < GAS_LONG_CANCEL_PARAMS_PERIOD_S:
      return
    self._param_t = now

    try:
      value = self._params.get(GAS_LONG_CANCEL_PARAM_KEY)   # 不带默认回退：None = 从未写过
    except Exception:   # 参数未声明 / 读取失败 ⇒ 保持当前值
      return
    if value is None:
      # dp: 首次运行把声明里的默认值落盘。UI 的 get_bool() 不走 params_keys.h 的
      # default ⇒ 不落盘就会出现"开关显示关、控制侧按默认开在跑"的分裂。
      try:
        self._params.put_bool(GAS_LONG_CANCEL_PARAM_KEY, bool(self.enabled))
      except Exception:
        pass
    else:
      new_enabled = self._as_bool(value)
      if new_enabled != self.enabled:
        self._param_note = (f"[GasLongCancel] param {GAS_LONG_CANCEL_PARAM_KEY}"
                            f" -> {'on' if new_enabled else 'off'}")
      self.enabled = new_enabled

    try:
      ms = self._params.get(GAS_LONG_CANCEL_RESUME_PARAM_KEY)
    except Exception:
      return
    if ms is None:
      try:
        self._params.put(GAS_LONG_CANCEL_RESUME_PARAM_KEY,
                         int(round(GAS_LONG_CANCEL_RESUME_S * 1000.0)))
      except Exception:
        pass
    else:
      seconds = self._to_float(ms, GAS_LONG_CANCEL_RESUME_S * 1000.0) / 1000.0
      self.resume_frames = self._frames_from_s(seconds)

  # --------------------------------------------------------------------------
  def update(self, gas_pressed: bool, engaged: bool, v_ego: float = 0.0, a_ego: float = 0.0) -> bool:
    """每帧调用一次（controlsd 的 100 Hz 主循环）。

    :param gas_pressed: CS.gasPressed
    :param engaged:     CC.enabled（= selfdriveState.enabled；刹车/按键取消后为 False）
    :param v_ego:       仅用于日志
    :param a_ego:       仅用于日志
    :return:            True ⇒ 本帧应把 CC.longActive 压成 False
    """
    now = time.monotonic()
    self._frame += 1
    self._refresh_params(now)

    gas = bool(gas_pressed)
    prev_gas = self.prev_gas
    self.prev_gas = gas

    if not (GAS_LONG_CANCEL_ENABLE and self.enabled and engaged):
      # 关闭 / 未激活。★ 需求 3 就落在这里：一旦 engaged 掉线（刹车取消、按键取消、
      # 超时取消……），立刻解除武装，之后踩油门 / 松油门都不再有任何作用 ——
      # 纵向能不能回来只由 CC.enabled 决定，即必须用户重新 engage。
      if self.armed:
        self.n_disarm += 1
      self.armed = False
      self.timer = 0
      self.suspended = False
      self.phase = PHASE_OFF
      self._maybe_log(now, gas, engaged, v_ego, a_ego)
      return False

    # 只在「油门上升沿」武装（详见模块头「已知取舍」）。
    # 若要改成"engage 那一刻脚已在油门上也算武装"，在此处加：
    #   or (gas and engaged and not self.prev_engaged)
    # 并在 update() 里维护 self.prev_engaged 与 self.prev_gas 同步更新即可。
    if gas and not prev_gas:
      self.armed = True
      self.n_suspend += 1

    if self.armed:
      if gas:
        self.timer = self.resume_frames
        self.suspended = True
        self.phase = PHASE_HELD
      elif self.timer > 0:
        self.timer -= 1
        self.suspended = True
        self.phase = PHASE_WAIT
      else:
        # 倒计时走完 ⇒ 恢复纵向。LoC / long_pid 的积分器已在挂起期间被 reset 过，
        # 这里是"从零重新接管"，不带挂起期的累积量。
        self.armed = False
        self.suspended = False
        self.n_resume += 1
        self.phase = PHASE_READY
    else:
      self.suspended = False
      self.phase = PHASE_READY

    self._maybe_log(now, gas, engaged, v_ego, a_ego)
    return self.suspended

  # --------------------------------------------------------------------------
  def _maybe_log(self, now: float, gas: bool, engaged: bool, v_ego: float, a_ego: float) -> None:
    """探针：相位跳变必打 + 挂起期间 1 Hz 心跳。

    ★ 变化检测签名**只放离散量**（phase / armed）—— 踩过的坑：签名里放连续量
    会让节流被击穿，变成每帧一条（`comma-swaglog-forensics` 硬规则）。
    """
    if not GAS_LONG_CANCEL_DIAG:
      return

    sig = (self.phase, self.armed)
    changed = sig != self._last_sig
    heartbeat = bool(self.suspended) and GAS_LONG_CANCEL_HEARTBEAT_S > 0.0 and now >= self._log_next_t
    note = self._param_note
    if not (changed or heartbeat or note is not None):
      return
    self._last_sig = sig
    self._param_note = None
    if heartbeat or changed:
      self._log_next_t = now + GAS_LONG_CANCEL_HEARTBEAT_S

    prefix = f"{note} " if note else ""
    msg = (f"{prefix}[GasLongCancel] phase={self.phase} gas={int(gas)} armed={int(self.armed)} "
           f"on={int(bool(self.enabled))} eng={int(bool(engaged))} t={self.timer} "
           f"resume_ms={int(round(self.resume_frames * DT_CTRL * 1000))} "
           f"v={self._to_float(v_ego, 0.0):.1f} a={self._to_float(a_ego, 0.0):.2f} "
           f"n_susp={self.n_suspend} n_res={self.n_resume} n_dis={self.n_disarm}")
    self._emit(msg)

  @staticmethod
  def _emit(msg: str) -> None:
    """惰性 import cloudlog（保持本模块零依赖），且**永不上抛异常**。"""
    try:
      from openpilot.common.swaglog import cloudlog   # noqa: PLC0415
      cloudlog.info(msg)
    except Exception:
      pass
