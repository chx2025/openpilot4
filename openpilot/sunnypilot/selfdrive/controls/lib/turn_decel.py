"""Blinker-triggered slow-down controller.

独立模块：打转向灯后让车在低速 (15 km/h) 段沿弯道行驶，避免高速进入弯道
撞护栏/路沿。规则：

  1. 检测到任一转向灯开启，累计 dt 达到 2s 后开始减速
  2. 减速度从初始 0.5 m/s² 慢慢往上升，最大 1.2 m/s²；持续减速到目标
     速度 15 km/h (4.167 m/s)；到目标后停减速但保持不加速
  3. 方向盘角度 > 15° 时暂停执行减速（让弯道本身的几何减速接管），
     但**保证不主动加速**（即使 base plan 想加速）
  4. 方向盘角度 > 60° 且 v_ego > 15 km/h 时**不主动加速**——独立约束，
     **与转向灯无关**（即使关闭了转向灯也要生效：U-turn、入库、
     螺旋匝道等极端大角度场景）
  5. 转向灯关闭：立刻停止减速、解除弯道减速相关的"不加速"约束；
     但若同时满足大角度条件（规则 4），仍保留 block_accel
  6. ACC (Normal) 模式和 Experimental Mode 都生效：在 longitudinal_planner
     里以 post-candidate-min 的方式作用到 output_a_target，
     不进入 candidates min 池，避免被 e2e/cruise 任何一方覆盖

设计取舍：
- 不进 candidates 池：candidates min 池是"谁更保守谁赢"的物理安全语义
  (traffic_stop / FCW / lead)；转向灯减速是驾驶员意图 + 弯道安全，不属于
  "碰撞避免"，不需要参与 min 投票
- 改成 min(post_a_target, turn_decel_a) + clamp(>= 0 阻止)：
  既不会让 turn_decel 触发不期望的硬减速（让 e2e 自由），也不会让
  turn_decel 已被触发时被其他 source 偷走"不加速"约束
- 渐进式减速度 ramp：从 0.5 m/s² 起，随时间慢慢升到 1.2 m/s² 封顶，
  避免一开始就急刹，又能在需要时果断减速到目标
- 始终允许 FCW/lead_brake 触发的更强减速通过：a_target 不会因为我们设了
  floor(0) 而阻止真刹车（刹车是负加速度，floor(0) 不影响）

公开 API：
  TurnDecelController.update(...) -> TurnDecelResult
  TurnDecelResult.a_target_override : float | None
                                 （None = 控制器不覆盖）
  TurnDecelResult.block_accel      : bool
                                 （True = 阻止任何正加速度）
  TurnDecelResult.active           : bool
                                 （True = 控制器处于主动减速期）
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass


# === [TurnDecel] 诊断探针（2026-09-27 加）==================================
# **只写日志，不参与任何控制判断**；本模块的行为逐位不变。
#
# 【为什么要它】本模块是纵向链路上**唯一没有任何留痕**的 post-min 覆盖模块
#   （旧的 [E2eTrace] 已移除）。而它的行为——打转向灯后主动减速到 15 km/h、
#   方向盘 >15° 时暂停、**一回正立刻恢复**——与用户 2026-09-27 实车反馈的
#   「右转弯然后直行的时候又一脚刹车」高度吻合。没有日志就只能猜。
#
# 【触发】相位或关键量跳变必打 + 非 idle 相位 1 Hz（100 Hz 控制回路，必须节流）。
#   注意 swaglog 最低只落 INFO ⇒ 只能用 info（debug 查不到）。
#
# 【字段】phase / act / blk / aOvr / blink（L、R、LR、on、-）/ blinkT（打灯累计
#   秒数，= 2.0 是 debounce 门槛）/ decelA（当前 ramp 减速度）/ v（km/h）/ steer（deg）
_PROBE_STATE: dict = {"t": 0.0, "sig": None}


def _emit_probe(res, *, left_blinker: bool, right_blinker: bool, blinker_on: bool,
                blinker_on_time: float, decel_a: float,
                v_ego: float, steering_angle_deg: float) -> None:
  """打一行 `[TurnDecel]` 探针。任何异常都吞掉——绝不能影响控制回路。"""
  try:
    now = _time.monotonic()
    ovr = None if res.a_target_override is None else round(res.a_target_override, 2)
    # ⚠️ 变化检测里**绝不能放连续变化的量**：`a_target_override` 在 deceling 期
    #   每帧都随 ramp 变（0.50→0.51→…）⇒ 若把它计入 sig，`changed` 恒为真 ⇒
    #   100 Hz 每帧一条，swaglog 会被瞬间淹掉（2026-09-27 真机冒烟实测：
    #   20 s 打了 71 条而非 ~20 条）。sig 只放**离散量**。
    sig = (res.phase, bool(res.active), bool(res.block_accel),
           bool(left_blinker), bool(right_blinker))
    changed = sig != _PROBE_STATE["sig"]
    if not changed and not (res.phase != "idle" and now - _PROBE_STATE["t"] >= 1.0):
      return
    _PROBE_STATE["t"] = now
    _PROBE_STATE["sig"] = sig

    bl = ('L' if left_blinker else '') + ('R' if right_blinker else '')
    if not bl:
      bl = 'on' if blinker_on else '-'
    a_ovr = 'None' if ovr is None else f'{ovr:.2f}'

    from openpilot.common.swaglog import cloudlog
    cloudlog.info(
      f"[TurnDecel] phase={res.phase} act={int(res.active)} blk={int(res.block_accel)} "
      f"aOvr={a_ovr} blink={bl} blinkT={blinker_on_time:.2f} decelA={decel_a:.2f} "
      f"v={v_ego * 3.6:.1f} steer={steering_angle_deg:+.1f}")
  except Exception:
    pass


# === 开关（dp 2026-10-20：UI 按钮，默认开）=================================
# 运行期开关（BOOL，默认 "1" = 开）。声明见 common/params_keys.h。
# 1 Hz 轮询 ⇒ 拨开关后约 1 s 内生效，不需要重启。
# 关闭时本模块逐位透明：不减速、不 block 加速，输出与未安装时完全一致。
TURN_DECEL_PARAM_KEY: str = "TurnDecel"
TURN_DECEL_PARAMS_PERIOD_S: float = 1.0


def _as_bool(value) -> bool:
  """★ Params.get() 返回 '0'/'1' 字符串（或 bytes），而 bool('0') 是 True。
  直接 bool() 会让开关永远关不掉，必须显式解析。"""
  if isinstance(value, bytes):
    value = value.decode()
  if isinstance(value, bool):
    return value
  return str(value).strip().lower() not in ('', '0', 'false', 'off', 'none')

# === 配置常量（按用户需求固定） ===
# 转向灯开启后等多久开始减速（秒）
BLINKER_DEBOUNCE_S: float = 2.0
# 初始减速度（m/s²）：减速刚启动即用这个温和值
DECEL_INITIAL_M_S2: float = 0.5
# 最大减速度（m/s²）：随时间慢慢从 initial 升到该值封顶
DECEL_MAX_M_S2: float = 1.2
# 减速度递增速率（m/s² 每秒，即 jerk）：决定"慢慢往上升"的快慢
DECEL_RAMPUP_M_S3: float = 0.2
# 目标速度：减速到此速度后停止减速（km/h）
TARGET_V_KPH: float = 15.0
TARGET_V_MS: float = TARGET_V_KPH / 3.6  # ≈ 4.167 m/s
# 方向盘角度 > 此值时暂停减速（deg）但仍阻止加速
STEERING_PAUSE_DEG: float = 15.0
# 方向盘角度 > 此值且 v_ego > 目标速度时阻止加速（deg，独立约束）
STEERING_BLOCK_ACCEL_DEG: float = 60.0

# ==== 驾驶员加速意图让位（2026-09-21 新增）================================
# 【现象】用户 2026-09-21 实车反馈，第二条给了可复现条件：
#   「转弯时再踩油门，等方向盘回正时就有可能出现加速无力」
#   「问题在于方向盘摆直了他也不加速」
#
# 【证据】`[E2eTrace]` 探针（设备 /data/ife/archive/probe_archive.jsonl）。
#   2026-09-21 13:05 北京连续 6 秒：
#     05:05:52 aE2e=+1.108 aTgt=0.000 src=4 turn=paused | gas=1 vEgo=19 vCruiseUI=55
#     05:05:53 aE2e=+1.224 aTgt=0.000 src=4 turn=paused | gas=1 vEgo=22 vCruiseUI=55
#   当时三条候选 aMpc=2.330 / aCruise=2.000 / aE2e=+1.224 **全为正**，
#   min() 数学上**不可能**得到 0 —— 唯一来源就是本模块的 block_accel。
#   全量 1034 帧统计（aE2e - aTgt 均值 / 「模型要 >+0.2 却被压成 0」帧数）：
#     idle（对照） 981 帧  +0.058 / 1
#     paused        24 帧  +0.740 / 14
#     big_angle_guard 19 帧 +0.640 / 9
#     waiting        9 帧   0.000 / 0
#   压制**全部**集中在非 idle 相位，对照组干净。
#
# 【机理】gas 按下时执行层被驾驶员油门盖住，本模块的压制看不出效果；一旦松油门
#   （典型时刻正是方向盘回正），压制立刻"接住"输出 -> 体感加速无力。
#   而 at_target（转向灯仍亮 + v_ego <= 15 km/h）会**持续** block，
#   表现为"方向盘摆直了也不加速"。
#
# 【判据】gas 踩下 —— 或 gas 释放后 TURN_DECEL_GAS_RELEASE_HOLD_S 秒内 ——
#   本模块**完全让位**（既不 block 也不减速）。
#
# 【为什么是安全的】踩油门是驾驶员对纵向的明确接管意图，此时
#   (a) 执行层本来就被油门覆盖，压制没有任何实际执行效果；
#   (b) 本模块本来就只 block **正**加速、从不参与制动。
#   所以让位不会放松任何减速/制动能力，只是把"想加速"的意愿还给驾驶员。
#   让位期间 **清零 blinker_on_time 且不累计**（见 update 内注释）：
#   否则宽限窗口一结束就立刻进入 deceling（主动减速到 15 km/h），
#   等于把上面那个 bug 推迟 2 秒再犯一遍。
#
# 【调参】嫌恢复慢 -> 调小 TURN_DECEL_GAS_RELEASE_HOLD_S；要立刻恢复 -> 设 0.0；
#   TURN_DECEL_DRIVER_OVERRIDE_ENABLE = False 一键回退成原行为。
TURN_DECEL_DRIVER_OVERRIDE_ENABLE: bool = True
TURN_DECEL_GAS_RELEASE_HOLD_S: float = 2.0


@dataclass
class TurnDecelResult:
  a_target_override: float | None  # 负值（减速）；None = 控制器不强制
  block_accel: bool               # True 时不允许任何正加速度
  active: bool                    # 控制器是否在主动减速
  phase: str                      # 'idle' / 'waiting' / 'paused' / 'deceling'
                                  #   / 'at_target' / 'big_angle_guard'
                                  #   / 'driver_override' / 'off'
                                  # 'off': UI 开关关闭（参数 TurnDecel = 0）
                                  # 'big_angle_guard': 转向灯关闭但大角度条件仍成立
                                  # 'driver_override': 驾驶员在踩油门 / 刚松开（见文件头）


class TurnDecelController:
  """无状态机，每帧由 longitudinal_planner 调 update()。

  状态在内部维护：累计 blinker 开启时间、当前减速 ramp 值、转向灯上一次的
  状态（用于检测上升沿和关闭事件）。
  """

  def __init__(self, params=None) -> None:
    self._blinker_on_time: float = 0.0    # 转向灯累计开启时长（dt 累加）
    self._blinker_on_last: bool = False   # 上一帧转向灯状态（用于检测下降沿）
    self._decel_a: float = 0.0            # 当前 ramp 后的减速度（0..DECEL_MAX_M_S2）
    self._gas_hold_left: float = 0.0      # [2026-09-21] 驾驶员让位窗口剩余时长（s）
    # ---- 运行期开关（dp 2026-10-20）----
    self._params = params                 # None ⇒ 首次 update 时懒加载
    self.enabled: bool = True             # 参数读不到时的兜底：开
    self._param_t: float = -TURN_DECEL_PARAMS_PERIOD_S   # 负值 ⇒ 第一帧立刻读一次

  def _refresh_params(self, now: float) -> None:
    """1 Hz 热刷新开关。任何异常都只影响"能不能开关"，绝不影响纵向控制。"""
    if self._params is None:
      try:
        from openpilot.common.params import Params
        self._params = Params()
      except Exception:
        self._params = False    # 建不出来就不再重试（保持兜底值）
        return
    if self._params is False:
      return
    if now - self._param_t < TURN_DECEL_PARAMS_PERIOD_S:
      return
    self._param_t = now
    try:
      raw = self._params.get(TURN_DECEL_PARAM_KEY)   # 不带默认回退：None = 从未写过
    except Exception:     # 参数未声明 / 读取失败 ⇒ 保持当前值
      return
    if raw is None:
      # dp: 首次运行把声明里的默认值（"1"）落盘。否则 UI 的 get_bool() 读不到
      # params_keys.h 的 default ⇒ 开关显示"关"，但控制侧按默认值在跑，两边打架。
      try:
        self._params.put_bool(TURN_DECEL_PARAM_KEY, bool(self.enabled))
      except Exception:
        pass
      return
    self.enabled = _as_bool(raw)

  def reset(self) -> None:
    """外部强制重置（disengage、模式切换等）。"""
    self._blinker_on_time = 0.0
    self._blinker_on_last = False
    self._decel_a = 0.0
    self._gas_hold_left = 0.0

  def update(
    self,
    *,
    blinker_on: bool,
    v_ego: float,
    steering_angle_deg: float,
    dt: float,
    gas_pressed: bool = False,
    left_blinker: bool = False,
    right_blinker: bool = False,
  ) -> TurnDecelResult:
    """每帧调用一次。

    Args:
      blinker_on: 左/右任一转向灯开启（True=开启）
      v_ego: 当前车速（m/s）
      steering_angle_deg: 方向盘绝对角度（deg）
      dt: 帧间隔（s），通常 0.05 (DT_MDL)
      gas_pressed: 驾驶员踩油门（carState.gasPressed）。见文件头「驾驶员加速意图让位」。
        默认 False 以保持旧调用方（含测试）行为不变。
      left_blinker / right_blinker: 仅**诊断探针**用来区分左右灯（2026-09-27 加）。
        默认 False ⇒ 旧调用方（含 tests/）行为逐位不变；**不参与任何控制判断**
        —— 控制只看 blinker_on（左转与右转在本模块行为完全相同）。

    Returns:
      TurnDecelResult 含 a_target_override / block_accel / active / phase
    """
    now = _time.monotonic()
    self._refresh_params(now)
    if not self.enabled:
      # UI 开关关闭 ⇒ 逐位透明（不减速、不 block）。顺带清掉累计态，
      # 重新打开时不会按"灯已开很久"的旧状态立刻减速。
      self.reset()
      res = TurnDecelResult(None, False, False, "off")
    else:
      res = self._update_impl(
        blinker_on=blinker_on,
        v_ego=v_ego,
        steering_angle_deg=steering_angle_deg,
        dt=dt,
        gas_pressed=gas_pressed,
      )
    _emit_probe(res,
                left_blinker=left_blinker,
                right_blinker=right_blinker,
                blinker_on=blinker_on,
                blinker_on_time=self._blinker_on_time,
                decel_a=self._decel_a,
                v_ego=v_ego,
                steering_angle_deg=steering_angle_deg)
    return res

  def _update_impl(
    self,
    *,
    blinker_on: bool,
    v_ego: float,
    steering_angle_deg: float,
    dt: float,
    gas_pressed: bool = False,
  ) -> TurnDecelResult:
    """真正的控制逻辑。**与加探针之前逐位相同**（探针在 update() 里包一层）。"""
    # ---- 0a. 驾驶员加速意图让位（最早评估：让位优先级高于一切本模块约束）----
    # 见文件头「驾驶员加速意图让位」。放在 big_angle_lock 之前，所以
    # big_angle_guard（>60° 安全锁）在驾驶员踩油门时同样让位 —— 理由见文件头：
    # 此时执行层已被油门覆盖，压制无执行效果，只会在松开瞬间变成"踩空"。
    if TURN_DECEL_DRIVER_OVERRIDE_ENABLE:
      if gas_pressed:
        self._gas_hold_left = TURN_DECEL_GAS_RELEASE_HOLD_S
      else:
        self._gas_hold_left = max(0.0, self._gas_hold_left - dt)
      if gas_pressed or self._gas_hold_left > 0.0:
        # 让位期间**清零并停止累计** blinker_on_time：
        # 否则宽限窗口结束的下一帧就会直接满足 debounce 进入 deceling（主动减速
        # 到 15 km/h），把上面那个 bug 推迟 2 秒再犯。清零 = 让位结束后重新计时。
        self._blinker_on_time = 0.0
        self._blinker_on_last = bool(blinker_on)
        self._decel_a = 0.0
        return TurnDecelResult(None, False, False, "driver_override")

    # ---- 0b. 大角度安全锁（独立于转向灯状态，最先评估）----
    # 方向盘 > 60° 且 v_ego > 15 km/h：不主动加速。
    # 必须在 blinker-off 分支之前评估——这是规则 4 的"与转向灯无关"。
    # 适用于：U-turn、入库、螺旋匝道等驾驶员不打转向灯的极端大角度场景。
    big_angle_block = (
      abs(steering_angle_deg) > STEERING_BLOCK_ACCEL_DEG
      and v_ego > TARGET_V_MS
    )

    # ---- 1. 跟踪 blinker 开启/下降沿 ----
    if blinker_on:
      self._blinker_on_time += dt
    else:
      # 转向灯关闭：完全退出减速流程（清 ramp / 清等待计时）。
      if self._blinker_on_last:
        self.reset()
      self._decel_a = 0.0
      # 但若大角度条件成立，仍要保留 block_accel（独立安全约束）。
      if big_angle_block:
        return TurnDecelResult(None, True, False, "big_angle_guard")
      return TurnDecelResult(None, False, False, "idle")

    self._blinker_on_last = True

    # ---- 2. 还在 2s debounce 等待期 ----
    if self._blinker_on_time < BLINKER_DEBOUNCE_S:
      # 等待期不减速，但大角度时仍 block accel
      return TurnDecelResult(None, big_angle_block, False, "waiting")

    # ---- 3. 方向盘 > 15° 暂停减速（弯道几何减速接管）----
    if abs(steering_angle_deg) > STEERING_PAUSE_DEG:
      # ramp 立即清零，避免恢复时阶跃
      self._decel_a = 0.0
      # 已通过 debounce 进入主动管理流程，规则 1 要求"保证不加速"。
      # 此分支涵盖 steering > 60° 的情况，所以不再叠加 big_angle_block：
      #   不论 steering 15~60°（弯道几何接管）还是 > 60°（安全锁），
      #   都属于"驾驶员打灯想拐"的管理态，统一阻断主动加速。
      return TurnDecelResult(None, True, False, "paused")

    # ---- 4. 已到目标速度 15 km/h：停减速但保持不加速 ----
    if v_ego <= TARGET_V_MS:
      self._decel_a = 0.0
      # 到目标后保持不加速（让弯道自己处理 + 驾驶员能加油门改主意）
      return TurnDecelResult(None, True, False, "at_target")

    # ---- 5. 主动减速 ----
    # 渐进式 ramp：首次进入给 initial 0.5，之后随时间慢慢升到 max 1.2。
    # 不会阶跃到 max（避免急刹），又能在需要时果断减速到目标。
    if self._decel_a < DECEL_INITIAL_M_S2:
      self._decel_a = DECEL_INITIAL_M_S2
    else:
      self._decel_a = min(DECEL_MAX_M_S2, self._decel_a + dt * DECEL_RAMPUP_M_S3)
    return TurnDecelResult(
      a_target_override=-self._decel_a,
      block_accel=True,
      active=True,
      phase="deceling",
    )
