# 交接说明 —— 第一次打开请看这一页

> **这份文件只解决一个问题**：你刚拿到这个仓库，**从哪开始**。
>
> 仓库有 150 个跟踪文件、10 份 docs、每个子目录还有自己的 README。
> 直接翻会迷路。下面按"你现在想干什么"分三条路。

---

## 零、这个项目是什么

**2026 嵌入式芯片与系统设计竞赛 · AMD 赛道**的参赛工程。

一块 **PYNQ-Z2**（XC7Z020）上跑**手势识别的图像预处理链**：

```
OV5640 摄像头 ─► PL 预处理链 ─► 96×96 灰度写进 DDR ─► PS 侧 CNN
                 (本项目)         ↑ 两侧唯一边界
```

**分工**：本仓库负责 **PL 侧**（采集 + 预处理 + 特征提取）；
**CNN 由另一侧负责**，边界只有 DDR 里那块 **9216 字节**。

---

## 一、先做这一步（**不需要 license，秒级**）

不管你想干什么，先确认这个工程在你机器上是活的：

```bash
# 在仓库根目录
bash rtl/run_iverilog.sh              # RTL 回归
bash sw/build_preproc_sim.sh          # PS 驱动主机自检
python host/test_overlay_offline.py   # PYNQ 驱动离线自检
```

**期望看到**（2026-09-25 实测）：

```
*** ALL RTL TESTS PASSED ***                  (3 通过, 0 失败)
*** PREPROC DRIVER SIM PASSED ***             (26 项检查全过)
*** OVERLAY OFFLINE TESTS PASSED ***          (70 项检查全过)
```

这三条**不需要 Vivado license、不需要板子**，每条都是秒级。

> 它们**已经挂进 CI**（`.github/workflows/rtl-sim.yml`）。
> 你 push 之后看仓库的 **Actions** 页 —— 那就是赛题 §3.3.4
> 「保证工程可由他人从零复现」的**直接证据**，比文档里写"验证过了"有力。

---

## 二、按你的目的选一条路

### 🅰 我要**跑出比特流 / 上板**（需要 Vivado license）

```bash
bash tools/rebuild_all.sh              # 全清缓存 → HLS → Vivado → 校验
bash tools/rebuild_all.sh --upload     # 再传板 + 核 md5
```

**前置**：Vitis + Vivado **2025.2**，器件 `xc7z020clg400-1`。
耗时 **20–40 分钟**。

**为什么必须用脚本、不能手敲两条命令** —— 三个坑它都堵了：

1. HLS 导出的 IP **版本号永远叫 `1.0`** → 新旧实现 VLNV 相同
   → Vivado 取了旧 IP **不报错，默默用错实现**
2. `gesture_comp/` 不在仓库里（被 ignore），且 `vivado/` 下有**三处** IP 缓存
3. 脚本**硬性校验三条**，缺一条 `exit 1`：
   `use_ila=0` / DMA 位宽 24 / 时序两侧为正

> ⚠ 跑之前确认没有 Vivado 进程存活，否则会锁文件导致脚本半途而废。
> ⚠ 别用 Ctrl-C 中断 —— 那会留下孤儿 `vivado.bat`，下次重建被它挡住。

**详细的**：`docs/board-bringup-guide.md`（上电前必读）

### 🅱 我要**调 CNN / 做 PS 侧**（**不需要板子**）

```bash
# 直接用现成的样例（golden 是硬件实测对过的）
python -c "
import numpy as np
rgb  = np.fromfile('samples/frame_640x480_rgb565.bin', dtype='<u2').reshape(480,640)
gray = np.fromfile('samples/golden_96x96_gray.bin',     dtype=np.uint8).reshape(96,96)
print(rgb.shape, gray.shape)"
```

**批量造训练数据**（`host/gesture_golden.py` 是可 import 的库，
输出与硬件**逐位一致**）—— 见 `docs/pl-to-ps-handoff.md` §2.3。

**接口契约（权威定义）**：`docs/architecture-contract.md` §3。

### 🅲 我要**改代码**

先读 **`docs/architecture-contract.md`** —— 里面记了**哪些方案被否掉了、为什么**，
避免你重复论证。

改之前要知道的几件事：

| 你改这里 | 必须同步改 |
|---|---|
| `src_hls/gesture_preproc.h` 的几何/寄存器常量 | `sw/preproc_driver.h`（PS 驱动只同步常量，**不 include 这个头文件**） |
| `src_hls/gesture_preproc.h` 或 `.cpp` 的**实现** | ⚠ **必须全清重建** —— IP 版本号不变，Vivado 会用旧的 |
| `vivado/constraints/video_io.xdc` 的引脚 | `docs/hardware-checklist.md` 的引脚表 |

---

## 三、仓库里的东西分别是什么

| 想看 | 去哪 |
|---|---|
| **先看这个** | `README.md`（根，含"最值钱的坑"清单） |
| 架构决策 / 接口契约 | `docs/architecture-contract.md` |
| 上板流程 / 采购 / 引脚 | `docs/board-bringup-guide.md`、`docs/hardware-checklist.md` |
| **板上日常操作速查**（传文件/传图片/常用指令） | `docs/board-cheatsheet.md` |
| 给 PS / CNN 侧的交付说明 | `docs/pl-to-ps-handoff.md` |
| 三次上板实测记录 | `docs/board-test-log-2026-09-2{1,2,3}.md` |
| 设计报告 | `report/design.md` |
| 可复用技能包（PYNQ 工具 / 踩坑 / 纠错方法论） | `skill/README.md` |
| 原 Sobel 参照工程（**冻结，别改**） | `legacy/sobel/` |

**代码目录**：

| 目录 | 内容 |
|---|---|
| `src_hls/` | HLS 处理链（`gesture_preproc.cpp`）+ 参考实现 + TB |
| `rtl/` | 手写 Verilog：DVP 采集 / SCCB / 异步 FIFO / IOBUF / 寄存器表 |
| `sw/` | PS 侧 C 驱动（`preproc_driver.c`） |
| `host/` | PC + 板上的 Python（golden / 驱动 / 造数据 / 推送） |
| `vivado/` | BD 脚本 + 约束 + **已含 bit 的 xsa** |
| `tools/` | `rebuild_all.sh` |
| `samples/` | 给 CNN 侧的样例数据（golden **硬件实测对过**） |

---

## 四、⚠ 拿比特流上板：`.bit` / `.hwh` 在 **xsa 里面**

`vivado/gesture_system/gesture_system.xsa` **已经含比特流**，不用先综合：

```bash
cd vivado/gesture_system
unzip -o gesture_system.xsa -d /tmp/xsa
# 里面是 bd_video.hwh（⚠ 不叫 gesture_system.hwh）
cp /tmp/xsa/gesture_system.bit /home/xilinx/        # 传到板子
cp /tmp/xsa/bd_video.hwh /home/xilinx/gesture_system.hwh   # ← 必须改名
```

> ⚠⚠ **`.hwh` 必须与 `.bit` 同名配对**。名字不一致时 PYNQ
> **不报「找不到 hwh」**，而是只认出 `default` 一个 IP，
> 然后 `g.ip['preproc']` 找不到 —— 现象很难往回追。

**当前这一版的 md5**（2026-09-25 重建，tag `v0.5-sccb-probe`）：

| 文件 | md5 |
|---|---|
| `gesture_system.bit` | `b3eb71359b686729dd6922bd40cd09f5` |
| `gesture_system.hwh` | `5e26bc5301995e322254736175fa0bcc` |

---

## 五、当前状态（如实说明）

| 部分 | 状态 |
|---|---|
| HLS 处理链 | ✅ csim + csynth + cosim 全过，所有循环 II=1 |
| RTL 外设 | ✅ 3/3 TB PASSED |
| BD / 时序 / 比特流 | ✅ WNS +0.198 / WHS +0.051 ns，DRC 0 Errors |
| **静态图喂入（方案 A）** | ✅ **板上实测：与 golden 逐字节一致，0/9216 不一致** |
| PS 侧驱动（C） | ✅ 主机自检 26/26；⚠ **裸机 C 驱动从未上机** |
| **摄像头通路** | ⚠ **未通**（卡在 SCCB，`cfg_error=1`）。**已不是关键路径** |
| HDMI 输出 | ⚠ 未做 —— **上板不要接 HDMI 线** |
| 自动跟随 ROI | ⚠ 单帧验证过；连续帧跟随**要等 USB 摄像头** |

> **一句话**：PL 侧功能已验证到**板上输出与 golden 逐字节一致**
> （静态图通路，三次不同输入 / 三种 ROI）。
> 摄像头是量程扩展，不是达标前提。

---

## 六、⚠ 几条会咬人的（都实际踩过）

| 坑 | 一句话 |
|---|---|
| **`use_ila` 必须为 0** | 开着它会让 BRAM 涨到 52.5%，并引入 **WHS −1.830 ns** 的 hold 违例（跨异步域，物理修不了）。提交前务必置 0 |
| **`.hwh` 要改名** | 见 §四 |
| **改 HLS 实现后必须全清重建** | IP 版本号恒为 1.0，Vivado 会用旧的且不报错 |
| **AXI DMA 默认 `C_SG_LENGTH_WIDTH` = 14 位** | 单次只搬 16 KB，**"传输完成"照常置位**，下游静默卡死 |
| **`sudo` 会重置 PATH** | 板上跑脚本要 `sudo -E /usr/local/share/pynq-venv/bin/python3 ...`（**Jupyter 不用**，内核本来就是 root） |
| **换比特流后要清 `__pycache__` + 重启内核** | PYNQ 的 overlay 不会因为换了文件就失效 |

完整清单见 `README.md` 的「最值钱的坑」与 `docs/board-cheatsheet.md` §五。

---

## 七、接下来最该做的三件事

按优先级：

1. **`use_ila=1` 构一版调试比特流**，读 SCCB 探针
   （`first_err_addr` / `nack_cnt`），回答摄像头那个二分问题
   —— ⚠ 探针**只接到 ILA，没有 AXI 寄存器**，`use_ila=0` 的正式版读不到
2. **与 CNN 侧敲定取景协议**（手占画面比例 / 小臂是否进框）
   —— 这是**反向依赖**：他采集时的构图决定 ROI 定多少，定了就不能改
3. **等 USB 摄像头到货后测连续帧**（`auto_roi` 的跟随稳定性、板上实际耗时）

---

## 八、许可

Apache-2.0，见 `LICENSE`。
