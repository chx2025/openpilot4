"""模型状态卡（侧边栏）—— 替换原 SUNNYLINK 状态卡。

状态来源：ui_state.chestnut_state（ChestnutState 枚举，见 ui/ui_state.py 的
_update_chestnut_state）。GPU 遥测：chestnutState 服务（hardwared 的
chestnut_state_thread 发布；tempC / powerDrawW / gpuUsagePercent 取自
chestnutGpuState，即 GPU SMU 内部遥测，不依赖 ASM2464 的 INA231 ——
三方底座没有供电遥测也能正常显示功耗/温度）。

显示规则（用户 2026-10-27 指定，2026-10-28 微调）：
  小模型（未检测到 eGPU）  -> label「小模型」，value 空
  大模型（各状态）          -> label「大模型」，value 显示具体状态：
      加载中  -> 进度百分比：36 秒 = 100% 时长估算，99% 封顶
      运行中  -> 功耗 + GPU 温度（如「87W 58°C」）
      未编译  -> 「未编译」
      失败    -> 「错误」

纯函数 + 只读输入，便于单测。
"""
from __future__ import annotations

LOADING_NOMINAL_S = 36.0  # 用户指定：加载进度以 36 s 为 100%


def build_model_sidebar_status(*, state_name: str, temp_c: float = 0.0,
                               power_w: float = 0.0, gpu_usage: int = 0,
                               loading_elapsed_s: float | None = None):
  """把一个采样时刻的模型状态压成侧边栏那一格要显示的 (label, value, severity, detail)。

  label: 第一行「小模型」/「大模型」（不再用英文 MODEL）。
  severity: good(白) / progress(蓝) / warning(黄) / danger(红) / disabled(灰)
  """
  if state_name == "disconnected":
    return "小模型", "", "disabled", "未检测到 eGPU，当前运行小模型"
  if state_name == "uncompiled":
    return "大模型", "未编译", "warning", "eGPU 已连接，但大模型尚未编译"
  if state_name == "loading":
    elapsed = loading_elapsed_s if loading_elapsed_s is not None else 0.0
    pct = max(0, min(99, int(elapsed / LOADING_NOMINAL_S * 100)))
    return "大模型", f"{pct}%", "progress", f"大模型加载中 {pct}%（按 36 s 估算）"
  if state_name == "failed":
    return "大模型", "错误", "danger", "大模型加载或运行失败，已回退小模型"
  if state_name in ("ready", "active"):
    # tempC/powerDrawW <= 0 视为没读到（capnp 默认 0），显示 -- 而不是 0，
    # 否则会被误读成"凉快/零功耗"，掩盖遥测失效。
    temp_text = f"{temp_c:.0f}°C" if temp_c > 0.0 else "--"
    power_text = f"{power_w:.0f}W" if power_w > 0.0 else "--"
    detail = f"大模型运行中 · 功耗 {power_text} · GPU 温度 {temp_text} · 占用 {gpu_usage}%"
    return "大模型", f"{power_text} {temp_text}", "good", detail
  return "模型", "…", "disabled", "状态未知"
