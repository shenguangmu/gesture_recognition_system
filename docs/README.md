# docs 索引

## 文档清单

| 文件 | 用途 | 读者 |
|---|---|---|
| **`architecture-contract.md`** | **分工边界 + 数据契约 + 决策记录** | 所有人（**先读这个**） |
| **`hardware-checklist.md`** | 采购清单 + 引脚映射 + 上电前验证 | 采购 / 接线的人 |
| **`board-bringup-guide.md`** | **上板实测流程 + 分级验证 + 硬阻塞清单** | 拿到板子的人（上电前必读） |
| **`board-test-log-2026-09-21.md`** | 首次上板实测：②③ 通过、DMA 根因、摄像头定位 | 继续排查的人 |
| **`board-test-log-2026-09-22.md`** | **第二次上板**：MMCM + 引脚映射两个根因、**摄像头仍未通** | **接着上次查时先读这个**（最新） |
| `gui-reproduction-guide.md` | 全程鼠标操作的复现流程 | 要用 GUI 的人 |

> 原 Sobel 工程的 GUI 复现指南（已实机验证过的那份）在
> **`legacy/sobel/docs/GUI复现指南.md`** —— 那个工程**一并放在本仓库
> 的 `legacy/sobel/` 下**。
> 它里面的 HLS 组件操作说明同样适用于本项目（只是文件名不同）。
>
> ⚠ **`legacy/` 下的文件名是本仓库唯一含中文的路径**。
> 它是**冻结的历史参照工程**（原 Sobel 加速器），按仓库设计**不应改动**。
> 本项目自己的目录与文件名**全部为纯英文**。

---

## 目录对照说明（赛题 §3.3.5.4）

赛题推荐结构与本仓库的对应关系：

| 赛题推荐 | 本仓库 | 说明 |
|---|---|---|
| `src/`（设计源码） | `src_hls/` + `rtl/` + `sw/` | 按实现方式拆分：HLS / 手写 Verilog / PS 侧 |
| `sim/` | `sim/` + `rtl/tb/` | 测试向量生成与比对 |
| `build/`（构建脚本 + 报告） | `vivado/` | 含 `create_project.tcl` 与综合实现报告 |
| `board/`（上板工程与实测） | `host/` + `docs/board-bringup-guide.md` | 前者是 PYNQ Python，后者是上板流程 |
| `data/`（测试数据与参考） | `host/` | golden 参考实现与对拍 |
| `skill/` | `skill/` | **同名** |
| `report/` | `report/` | **同名** |
| — | `docs/` | 本仓库额外：架构契约、采购清单、本指南 |

---

## 四个文件分别在回答什么问题

### `architecture-contract.md` —— 为什么这么做

- PL / PS 分工（**为什么 CNN 不放 PL**）
- 与 CNN 侧的 DDR 契约（96×96 uint8 灰度）
- **BD 集成决策**（为什么从 DDR 分叉而不是从 `dvp_capture` 分叉）
- 每个决策的推理过程与备选方案

**改动任何架构决定前先读这个** —— 里面记录了哪些方案被否掉了、
以及为什么，避免重复论证。

### `hardware-checklist.md` —— 买什么、怎么接

- 购物清单（含型号、预估价格）
- **引脚映射表**（模块连接器 → PYNQ 端口 → FPGA 引脚）
- **上电前必查清单**

> ⚠ 关键风险：**摄像头模块的原理图是"镜像编号"**，
> 引脚映射按镜像解读推导，但**未经实测**。
> 插错方向会 **3V3/GND 反接烧板**。
> 验证步骤在 §1.2 和 `vivado/constraints/video_io.xdc` 末尾。

### `gui-reproduction-guide.md` —— 怎么用鼠标点出来

从**加入 RTL 源**到**综合实现**，每步都有鼠标操作说明，
同时给出对应的 Tcl 命令（便于出错时对照）。

**不含** HLS 部分 —— 那一块去看 `legacy/sobel/docs/GUI复现指南.md`
（组件式 vs 直接开 HLS 工程、csim 的"假成功"陷阱等，都是通用的）。

### `board-test-log-2026-09-22.md` —— **第二轮实测（最新）**

查出并修复**两个根因**，但摄像头**仍未通**：

- **MMCM 参数被静默忽略**（`[IP_Flow 19-3374]` 只给 WARNING）→
  解药是 `CONFIG.OVERRIDE_MMCM {true}`，**不是** `USE_FREQ_SYNTH`
- **引脚映射按"镜像"推导，14 个信号全错**（XCLK/PCLK 都接到空脚上）
  → **决定性证据是模块丝印**（`5/6/11/12 = GND/VCC/GND/VCC` = 标准 Pmod）
- **PCLK 与 CCIO 不相交** → 布线失败 → `CLOCK_DEDICATED_ROUTE FALSE`
- 摄像头仍不出图（帧计数 0），待查见文末
- ⚠ **两次蓝屏**（Vivado 与硬件交互时），未解决

### `board-test-log-2026-09-21.md` —— 首轮实测

- **②③ 已通过**：DDR 自检 + 只跑预处理链，`11/11` 全过，单帧 0.005 s
- **DMA 根因**：`C_SG_LENGTH_WIDTH` 默认 14 位 → 只传前 16 KB
  （判据：**读回值 = 写入值 mod 16384**）
- **环境踩坑**：SD 卡未烧、网卡配错、`sudo` 清空环境

---

## 当前项目的完整入口

```
README.md                          ← 总入口，含进度表与一键回归命令
├── docs/architecture-contract.md          ← 先读这个（做什么、为什么）
├── docs/hardware-checklist.md            ← 买什么、怎么接
├── docs/gui-reproduction-guide.md ← 怎么点出来
├── docs/board-test-log-2026-09-21.md ← 已上板：实测记录（先读这个）
│
├── src_hls/README.md              ← HLS 处理链（语义契约 + cosim 排查）
├── rtl/README.md                  ← 手写 Verilog（含验证盲区说明）
├── sw/README.md                   ← PS 侧驱动
├── host/README.md                 ← PC 侧工具（golden / 对拍 / overlay）
├── vivado/README.md               ← BD 脚本（含 19 个踩坑记录）
└── legacy/README.md               ← 原 Sobel 参照工程（为什么在这）
```

**推荐阅读顺序**：

1. 本文件 → `architecture-contract.md`（搞清做什么、为什么）
2. `hardware-checklist.md`（买什么、怎么接，含上电前验证）
3. `board-bringup-guide.md`（怎么上板)
4. 各模块 README（具体实现与各自的坑）
5. **接着上次排查**：`board-test-log-2026-09-21.md`（**实测记录，含未解项**）
