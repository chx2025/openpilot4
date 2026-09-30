# 显卡坞不断电、只重启 C3 后 eGPU 识别不到（Cinque v3）

排查记录与修复说明。若再遇到"重启后 eGPU 没了"，先看第 1、4 节，不要从零开始查。

## 1. 症状

- 显卡坞（eGPU/USB 桥）保持供电，只重启 C3：开机后主机**从不重新枚举**显卡，`UsbGpuPresent=0`，
  设置页 / web 显示未连接；把坞**断电再上电**就恢复。
- 因为 Cinque v3 是 `precompiled_only` 交付（必须用预编译产物，不回退本地编译），eGPU 一旦没被识别，
  v3 直接不可用 —— 所以这个 USB 问题和"v2/v3 表现不一样"经常缠在一起。
- 2026-09-30 定位并修复（见第 4 节）。修复前是稳定复现的。

## 2. 根因

v3 的 runtime（服务器上的 `precompiled-runtime.tar.gz`，解压成
`/data/media/0/carrot/models/precompiled/<model-sha>/runtime-<runtime-sha16>/`）与桥之间有**两个同时用同一
条 SuperSpeed 链路的使用者**：

1. AMD 驱动通过 tinygrad 的 HCQ ring 异步提交命令；
2. tinygrad 自己的 USB 后端为同一块 MMIO 窗口做同步控制传输（`tinygrad/runtime/support/usb.py`
   的 `USBMMIOInterface.__getitem__/__setitem__`）。

坞一直带电时，桥**保留上一次主机（上一次启动）的链路状态**。C3 重启后，第一次 python 侧 MMIO 访问会
压在上次主机仍未撤掉的 ring 提交上，链路就此卡死，主机不再重新枚举 —— 表现就是"坞要断电才行"。

修复：每次 MMIO 访问前先 `Device[HCQ_RUNTIME_DEV.value].synchronize()` 把 ring 排空，把两个使用者串行化。

## 3. 排查时容易走岔的地方（记下来省时间）

- `usbgpu_present()` 要求 **SuperSpeed ≥ 5000 Mbps**：桥掉到 USB 2.0 枚举同样算"未识别"。
- `check_usbgpu` 的探测子进程依赖 `sitecustomize` 装上 tinygrad 补丁（`e70b33f` 起探测不再自己先装）；
  若补丁没生效，健康 GPU 也会被判成 `GPU incompatible`。
- 三个分支（`egpucp` / `carrot-wip` / `carrot-wip-merge`）历史上**都没有**过自动复位逻辑：不是丢代码，
  而是这个卡死状态一直存在，只是 v3 让它变得可见。
- 设备上 manager/modeld/ui 都以 `comma` 用户运行，**USB 复位必须走 `sudo -n`**（设备免密）。
- eGPU 挂在 `usb4`（`a600000.ssusb` / `xhci-hcd.1.auto`），panda 在 `usb1` —— **不同控制器**，
  复位 `usb4` 不会动到 panda。可用层级：`4-1/authorized`、`4-1/remove`、`usb` 驱动 unbind/bind、
  `xhci-hcd` 平台驱动 unbind/bind。
- 取证据：`/sys/bus/usb/devices/4-1/speed`、`dmesg | grep -i -e usb -e xhci`、
  `cat /data/params/d/UsbGpuPresent`、artifact 目录里的 `last_failure.json` / `boot_validation.json`。

## 4. 修复与落地方式（两处，别只做一处）

**A. 运行时补丁（修已有设备）**

`openpilot/selfdrive/modeld/precompiled_fetch.py`：

- `patch_runtime_usb_link()`：把排空语句写进已解压 runtime 的 `usb.py`（幂等，每次开机由
  `fetch_after_onnx()` 重新应用）。文本在 `_USB_LINK_PATCHES` 常量里，注释里写了完整根因。
- `patch_runtime_firmware_paths()`：原有的固件搜索路径补丁，同一位置。

这一条是**已有设备真正的修复**：`ensure_precompiled()` 在本地副本校验通过时会直接短路，
不会重新读服务器 catalog，所以**只换服务器上的包，已下载过的设备不会自动更新**。

**B. 服务器包（修全新下载/新设备）**

重打 `precompiled-runtime.tar.gz` 的步骤（必须用 `precompiled_fetch` 里的同一份补丁文本，不要手改）：

1. 下载镜像上的 `precompiled.json` 与 `precompiled-runtime.tar.gz`，先校验 tarball 与 catalog 里的
   `runtime.sha256` 一致；
2. 解压到临时目录，对其调用 `patch_runtime_firmware_paths(runtime_dir=...)` 与
   `patch_runtime_usb_link(runtime_dir=...)`（可按文件路径加载 `precompiled_fetch.py`，它模块级只有标准库
   import，无需整棵 openpilot 树）；
3. 重新打包：只含文件、按名排序、mtime/uid/gid 固定，保持与原包相同的结构；解包自检 `usb.py` 中
   `one ring, one driver on the link` 出现两次、`helpers.py` 中出现 `_FORK_FW_WRAPPED`；
4. 用新 tarball 的 size/sha256 更新 catalog 的 `runtime` 字段。

2026-09-30 那次重打的脚本是一次性工具，随交付包放在仓库外（`egpu_runtime_package/`），没有入库；
按上面 4 步随时可以重建。当次产物：

- size `2731184` → `2732625`
- sha256 `a25b81e90d5259c27bc49b3a93ab499c1909a33e133cba974cf023cd94727eeb`
  → `47b88f122e4be92827e9245320ef18007258b5ac3b9ac7d5e359c4cb72eb14c2`

上传到镜像的 `models/cinque-v3/`（**pkl 不动**：runtime 是独立产物，sha 变了设备会重新下载并解压）。

## 5. 验证

- 设备：`grep -c "one ring, one driver on the link" <runtime>/tinygrad/runtime/support/usb.py` 应为 2；
  重启 C3 后 `UsbGpuPresent=1`、`/sys/bus/usb/devices/4-1/speed` 为 `5000`。
- 想强制走服务器包路径：删掉 `.../precompiled/<model-sha>/` 后重启，观察它重新下载并解压出新
  `runtime-<sha16>` 目录。

## 6. 现状与注意

- 2026-09-30 在实车上以运行时补丁验证通过（补丁写入 18:25:58，重启 C3 后 eGPU 正常，v3 生效）。
  样本仍偏少：若再次复现，先按第 3 节取 `dmesg` 与 `4-1` 状态，再判断是链路卡死还是别的原因。
- 排空会给每次 MMIO 访问加一次 ring 同步；USB 附着的 AMD 每次寄存器访问本来就是一次 USB 往返，
  实测帧率正常，若将来怀疑它，先量 `usb.py` 的调用频率再动。
