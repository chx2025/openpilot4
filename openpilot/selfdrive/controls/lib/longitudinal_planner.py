#!/usr/bin/env python3
import math
import numpy as np

import openpilot.cereal.messaging as messaging
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, get_accel_from_plan, should_stop
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
from openpilot.common.swaglog import cloudlog
from openpilot.common.params import Params

from openpilot.sunnypilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlannerSP
from openpilot.sunnypilot.selfdrive.controls.lib.turn_decel import TurnDecelController

A_CRUISE_MAX_BP = [0., 2.77, 5.55, 8.33, 11.11, 13.89, 16.6, 19.4, 22.22, 25, 27.78, 33.33]
A_CRUISE_MAX_VALS = [1.05, 0.9, 0.80, 0.65, 0.50, 0.40, 0.35, 0.35, 0.35, 0.33, 0.31, 0.29]
J_CRUISE_VALS = [1.05, 0.9, 0.80, 0.65, 0.50, 0.40, 0.35, 0.35, 0.35, 0.33, 0.31, 0.29]
A_CRUISE_MIN = -1.2
CONTROL_N_T_IDX = ModelConstants.T_IDXS[:CONTROL_N]
ALLOW_THROTTLE_THRESHOLD = 0.4
MIN_ALLOW_THROTTLE_SPEED = 2.5

# Lookup table for turns
_A_TOTAL_MAX_V = [1.7, 3.2]
_A_TOTAL_MAX_BP = [20., 40.]

# 实验模式（e2e）下的正加速上限。
#
# 原逻辑用 opendbc 的 ACCEL_MAX（= 2.0，那是**车辆物理上限**，用于最终
# np.clip 与 MPC 约束，不能动）：`max_accel = ACCEL_MAX if e2e else 查表`。
# 也就是说实验模式下 a_cruise 的加速上限直接顶到 2.0（≈0.20 g），
# 比非实验模式的查表值（低速 1.10 → 高速 0.29）猛得多，畅通路段模型
# 会把 desiredAcceleration 拉满，体感偏冲。
#
# 这里单独把 e2e 分支收窄到 1.3（≈0.13 g）。
# ★生效机制：a_cruise 永远在 candidates 里且 min() 取小 ⇒ 只要压住
#   a_cruise 的上限，最终 output_a_target 就不可能超过它，
#   无论 MPC 轨迹或 e2e 候选给多大都会被 min 掉。
#   （所以不需要去改 long_mpc.py 的求解约束，也就避开了动求解器的风险。）
E2E_MAX_ACCEL = 1.3

def get_max_accel(v_ego):
  return np.interp(v_ego, A_CRUISE_MAX_BP, A_CRUISE_MAX_VALS)

def get_coast_accel(pitch):
  return np.sin(pitch) * -5.65 - 0.3  # fitted from data using xx/projects/allow_throttle/compute_coast_accel.py

def get_cruise_accel(e2e, v_cruise, v_ego, a_cruise_prev, angle_steers, CP, dt, accel_coast, allow_throttle):
  # 实验模式走 E2E_MAX_ACCEL(1.3)，非实验模式走速度查表（低速 1.10 → 高速 0.29）。
  # 见上方 E2E_MAX_ACCEL 的说明：这里压住 a_cruise 的上限，就压住了整条链路的正加速上限。
  max_accel = E2E_MAX_ACCEL if e2e else get_max_accel(v_ego)

  if not e2e:
    a_total_max = np.interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
    a_y = v_ego ** 2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
    a_x_allowed = math.sqrt(max(a_total_max ** 2 - a_y ** 2, 0.))
    max_accel = min(max_accel, a_x_allowed)
    if not allow_throttle:
      clipped_accel_coast = max(accel_coast, ACCEL_MIN)
      coast_limit = np.interp(v_ego, [MIN_ALLOW_THROTTLE_SPEED, MIN_ALLOW_THROTTLE_SPEED*2], [max_accel, clipped_accel_coast])
      max_accel = min(max_accel, coast_limit)

  target_accel = np.clip(v_cruise - v_ego, A_CRUISE_MIN, max_accel)
  j_cruise = np.interp(v_ego, A_CRUISE_MAX_BP, J_CRUISE_VALS)
  target_accel = float(np.clip(target_accel, a_cruise_prev - j_cruise * dt, a_cruise_prev + j_cruise * dt))

  return target_accel


class LongitudinalPlanner(LongitudinalPlannerSP):
  def __init__(self, CP, CP_SP, init_v=0.0, init_a=0.0, dt=DT_MDL):
    self.CP = CP
    self.mpc = LongitudinalMpc(dt=dt)
    LongitudinalPlannerSP.__init__(self, self.CP, CP_SP, self.mpc)
    self.fcw = False
    self.dt = dt
    self.allow_throttle = True

    self.v_desired_filter = FirstOrderFilter(init_v, 2.0, self.dt)
    self.a_cruise = init_a
    self.output_a_target = init_a
    self.output_should_stop = False

    # 打灯减速 + 大角度限加速（sunnypilot 追加，见 turn_decel.py）
    self.turn_decel = TurnDecelController()


    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)
    self.j_desired_trajectory = np.zeros(CONTROL_N)

  def update(self, sm):
    LongitudinalPlannerSP.update(self, sm)

    if len(sm['carControl'].orientationNED) == 3:
      accel_coast = get_coast_accel(sm['carControl'].orientationNED[1])
    else:
      accel_coast = ACCEL_MAX

    v_ego = sm['carState'].vEgo
    v_cruise_kph = min(sm['carState'].vCruise, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS
    if sm['controlsState'].forceDecel:
      v_cruise = 0.0

    long_control_off = sm['controlsState'].longControlState == LongCtrlState.off

    # Reset current state when not engaged, or user is controlling the speed
    reset_state = long_control_off if self.CP.openpilotLongitudinalControl else not sm['selfdriveState'].enabled
    # PCM cruise speed may be updated a few cycles later, check if initialized
    v_cruise_initialized = sm['carState'].vCruise != V_CRUISE_UNSET
    reset_state = reset_state or not v_cruise_initialized

    throttle_probs = sm['modelV2'].meta.disengagePredictions.gasPressProbs
    throttle_prob = throttle_probs[1] if len(throttle_probs) > 1 else 1.0
    self.allow_throttle = throttle_prob > ALLOW_THROTTLE_THRESHOLD or v_ego <= MIN_ALLOW_THROTTLE_SPEED

    steer_angle_without_offset = sm['carState'].steeringAngleDeg - sm['vehicleParameters'].angleOffsetDeg

    if reset_state:
      self.v_desired_filter.x = v_ego
      self.output_a_target = np.clip(sm['carState'].aEgo, ACCEL_MIN, ACCEL_MAX)
      self.a_cruise = self.output_a_target
      # 未接管/车速未就绪时清掉打灯减速的累计状态，避免再接管时
      # 立刻按「转向灯已开很久」的旧状态减速
      self.turn_decel.reset()
      # 红灯辅助：未接管 = 上下文已丢失（可能换了一趟行程），必须从 CRUISE
      # 重新识别，不能把上一段的停等带过来（其滤波器按设计保留，见 traffic_stop.py）
      self.traffic_stop.reset()

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))

    # No change cost when user is controlling the speed, or when standstill
    prev_accel_constraint = not (reset_state or sm['carState'].standstill)

    # ── 红灯 / 停止标志「虚拟停止线」（见 sunnypilot/.../traffic_stop.py）────────
    # 位置三要素，改前必读：
    #  * 必须在 update_targets() **之前** —— 它产出的 output_v_target 要参与
    #    targets 字典的 min() 竞选（压低 v_cruise ⇒ 压低 a_cruise）；
    #  * 用**本帧原始 v_cruise**（上面刚算出、尚未被 SP 层降级）⇒ 无循环依赖；
    #  * 必须在 mpc.update() **之前** —— stop_dist_m 要作为第 3 栏障碍物传进去。
    # 为什么"实验模式下也有用"：主 planner 的 min(candidates, key=a_target)
    # 只取更保守的一方。本模块让 aMpc 与 a_cruise 同时更保守 ⇒ 天然压得住
    # e2e 的正加速，同时**从不削弱** e2e 自己更保守的判断（min 只会取更小值）。
    self.traffic_stop.update(
      model_x_traj=sm['modelV2'].position.x,
      model_y_traj=sm['modelV2'].position.y,
      model_v_traj=sm['modelV2'].velocity.x,
      steering_angle_deg=steer_angle_without_offset,
      # [2026-09-28 阿丽] 恢复真实油门 —— 这条不是「踩油门时额外做动作」，
      # 而是「踩油门时不要拦驾驶员」的保护底线：红灯辅助的灯色是从模型轨迹
      # 反推的（它并不看灯），一旦误判进 STOPPING/STOPPED，驾驶员踩油门必须
      # 能立刻接管。它只会让模块**少减速**，不可能制造「无故刹车」。
      # （踩油门挂起纵向走 gas_long_cancel.py 的 CC.longActive，两者不冲突。）
      gas_pressed=bool(sm['carState'].gasPressed),
      left_blinker=bool(sm['carState'].leftBlinker),
      lead_present=bool(sm['radarState'].leadOne.present),
      d_rel=float(sm['radarState'].leadOne.dRel),
      v_ego=v_ego,
      a_ego=float(sm['carState'].aEgo),
      v_cruise=v_cruise,
      dt=self.dt,
      # [2026-09-27] 仅供 [TrafficStop] 探针的 blinkR= 字段（不参与控制）。
      right_blinker=bool(sm['carState'].rightBlinker),
    )
    if self.traffic_stop.log is not None:
      cloudlog.info(self.traffic_stop.log)

    # Get new v_cruise and a_target from Smart Cruise Control and Speed Limit Assist
    v_cruise, self.output_a_target = LongitudinalPlannerSP.update_targets(self, sm, self.v_desired_filter.x, self.output_a_target, v_cruise)

    # ★ 诊断探针（2026-09-27 加）：红灯辅助**是否真的赢下**候选池。
    #   赢家由 SP 层 `min(targets, key=lambda k: targets[k][0])` 按 v_target 选出。
    #   纵向链路原本没有"谁赢"的留痕 ⇒ 排查"半路莫名减速"只能靠这一条。
    #   只在赢的时候打（1 Hz 节流）；没赢就静默，不吵。
    if self.source == LongitudinalPlanSource.trafficStop:
      self._ts_win_t = getattr(self, '_ts_win_t', 0.0) + self.dt
      if self._ts_win_t >= 1.0:
        self._ts_win_t = 0.0
        cloudlog.info(f"[LongSrc] trafficStop WINS vTgt={v_cruise:.2f} "
                      f"aTgt={self.output_a_target:+.2f} "
                      f"ts_vTgt={self.traffic_stop.output_v_target:.2f} "
                      f"d={self.traffic_stop.stop_dist_m}")

    self.mpc.set_weights(prev_accel_constraint, personality=sm['selfdriveState'].personality)
    self.mpc.set_cur_state(self.v_desired_filter.x, self.output_a_target)
    self.mpc.update(sm['radarState'], personality=sm['selfdriveState'].personality,
                    traffic_stop_obstacle_m=self.traffic_stop.stop_dist_m)

    self.v_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.v_solution)
    self.a_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.a_solution)
    self.j_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC[:-1], self.mpc.j_solution)

    # TODO counter is only needed because radar is glitchy, remove once radar is gone
    self.fcw = self.mpc.crash_cnt > 2 and not sm['carState'].standstill
    if self.fcw:
      cloudlog.info("FCW triggered")

    # Save starting point for next iteration
    a_prev = self.output_a_target

    action_t =  self.CP.longitudinalActuatorDelay + DT_MDL
    output_a_target_mpc = get_accel_from_plan(self.v_desired_trajectory, self.a_desired_trajectory, CONTROL_N_T_IDX,
                                              action_t=action_t)
    output_should_stop_mpc = should_stop(v_ego, output_a_target_mpc)
    output_a_target_e2e = sm['modelV2'].action.desiredAcceleration
    output_should_stop_e2e = sm['modelV2'].action.shouldStop

    is_e2e = self.is_e2e(sm)

    self.a_cruise = get_cruise_accel(is_e2e, v_cruise, v_ego,
                                     self.a_cruise, steer_angle_without_offset, self.CP, self.dt,
                                     accel_coast, self.allow_throttle)
    cruise_should_stop = should_stop(v_ego, self.a_cruise)

    candidates = [(output_a_target_mpc, self.mpc.source, output_should_stop_mpc),
                  (self.a_cruise, LongitudinalPlanSource.cruise, cruise_should_stop)]
    if is_e2e:
      candidates.append((output_a_target_e2e, LongitudinalPlanSource.e2e, output_should_stop_e2e))

    output_a_target, self.mpc.source, _ = min(candidates, key=lambda c: c[0])
    self.output_should_stop = any(should_stop for _, _, should_stop in candidates)

    # 打灯减速 + 大角度限加速（sunnypilot 追加）。
    # 放在 candidates 的 min() 之后、np.clip 之前：
    #   - min() 保证不会盖过 candidates 里更保守的一方（FCW/前车/MPC 该刹还是刹）
    #   - 同时不会被 e2e 的正加速度覆盖掉"不许加速"的约束
    # 两种意图：打转向灯后弯道减速；以及方向角度过大时（与转向灯无关）不许加速
    blinker_on = bool(sm['carState'].leftBlinker or sm['carState'].rightBlinker)
    turn_decel_res = self.turn_decel.update(
      blinker_on=blinker_on,
      v_ego=v_ego,
      steering_angle_deg=steer_angle_without_offset,
      dt=self.dt,
      # [2026-09-27] 仅供 [TurnDecel] 诊断探针区分左右灯 —— **不参与控制**
      # （控制仍只看上面的 blinker_on）。左转/右转在本模块行为完全相同，
      # 但日志里必须能分辨，否则"打灯右转的一脚"没法归因。
      left_blinker=bool(sm['carState'].leftBlinker),
      right_blinker=bool(sm['carState'].rightBlinker),
    )
    if turn_decel_res.a_target_override is not None:
      output_a_target = min(output_a_target, turn_decel_res.a_target_override)
    if turn_decel_res.block_accel and output_a_target > 0.0:
      output_a_target = 0.0


    self.output_a_target = np.clip(output_a_target, ACCEL_MIN, ACCEL_MAX)

    self.v_desired_filter.x = self.v_desired_filter.x + self.dt * (self.output_a_target + a_prev) / 2.0

  def publish(self, sm, pm):
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_checks()

    longitudinalPlan = plan_send.longitudinalPlan
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.logMonoTime['modelV2']
    longitudinalPlan.solverExecutionTime = self.mpc.solve_time

    longitudinalPlan.speeds = self.v_desired_trajectory.tolist()
    longitudinalPlan.accels = self.a_desired_trajectory.tolist()
    longitudinalPlan.jerks = self.j_desired_trajectory.tolist()

    longitudinalPlan.hasLead = sm['radarState'].leadOne.present
    longitudinalPlan.longitudinalPlanSource = self.mpc.source
    longitudinalPlan.fcw = self.fcw

    longitudinalPlan.aTarget = float(self.output_a_target)
    longitudinalPlan.shouldStop = bool(self.output_should_stop)
    longitudinalPlan.allowBrake = True
    longitudinalPlan.allowThrottle = bool(self.allow_throttle)

    pm.send('longitudinalPlan', plan_send)

    self.publish_longitudinal_plan_sp(sm, pm)
