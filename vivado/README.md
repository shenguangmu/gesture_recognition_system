# vivado 索引

> 📄 **综合与实现报告摘要见 [`build-report.md`](build-report.md)** ——
> 资源占用 / 时钟频率 / 关键性能指标，赛题 §3.3.5.1 要求的三项。

## 本目录只有一个 BD：`bd_video`

视频流水线 + 预处理链，**已跑通到生成比特流**。

| 脚本 | 作用 |
|---|---|
| `bd_video.tcl` | 建 Block Design（VDMA + 预处理链 + SCCB + Clocking Wizard） |
| `create_project.tcl` | **一键建工程**：从零到比特流 + XSA |
| `test_bd_video.tcl` | 只建 BD 并 validate（不综合，约 1 分钟） |
| `test_video_io_xdc.tcl` | 加约束 + 综合（验证 XDC 真的生效） |
| `constraints/video_io.xdc` | 主约束文件 |
| `constraints/video_io_hdmi_tmp.xdc` | ⚠ HDMI 临时豁免（见「HDMI 通路」节） |
| `constraints/hdmi_drc_hook.tcl` | ⚠ write_bitstream 的 pre-hook |

> **原 Sobel 的 `bd_sobel.tcl` 已移到 `legacy/sobel/vivado/`** ——
> 它作为"已验证的参照"与本项目并存，但不再参与本工程的构建。
> 原因见 `docs/architecture-contract.md` §4.1。

## 构建与验证

从零建工程 → 综合 → 实现 → 比特流 → XSA（**约 20–40 分钟**）：

```bash
vivado -mode batch -source vivado/create_project.tcl
```

只想快速检查 BD 是否合法（约 1 分钟，不综合）：

```bash
vivado -mode batch -source vivado/create_project.tcl -tclargs --synth 0
```

分步验证：

```bash
vivado -mode batch -source vivado/test_bd_video.tcl      # BD + validate
vivado -mode batch -source vivado/test_video_io_xdc.tcl  # 约束 + 综合
```

> ⚠ **不要在 `vivado` 前面加 `bash`** —— 本目录里就有个 `vivado/` 目录，
> `bash vivado` 会让 bash 去执行那个目录，报 `Is a directory`。

所有脚本失败时会立刻 `exit 1` 并打印错误，适合接进 CI。

---

## 约束文件

| 文件 | 内容 | 状态 |
|---|---|---|
| `constraints/video_io.xdc` | **主约束**：PCLK 时钟 + 跨时钟域 + 摄像头引脚 | ✅ 生效（综合后实测确认） |
| `constraints/video_io_hdmi_tmp.xdc` | ⚠ **临时**：HDMI 端口的 DRC 豁免 | ⚠ 方案 B 做完后删 |
| `constraints/hdmi_drc_hook.tcl` | ⚠ write_bitstream 的 pre-hook | ⚠ 同上 |

> 原 Sobel 的 `sobel_io.xdc` 已随它一起移到 `legacy/sobel/vivado/constraints/`。

### video_io.xdc 的分层

| 层 | 内容 | 状态 |
|---|---|---|
| 一 | PCLK 时钟定义（24 MHz） | ✅ 已启用，验证生效 |
| 二 | 跨时钟域声明（PCLK ↔ sysclk 异步） | ✅ 已启用，验证生效 |
| 三 | **摄像头引脚（PMOD-CAMERA v1.0）** | ✅ **已启用**（14 个信号） |
| 四 | HDMI TMDS 引脚 | ⏸ 待 BD 补齐端口（已留模板） |
| 五 | 调试信号 false_path | ✅ 已启用 |

第三层的端口名是 `io_*` 而不是 `cam_*` —— 因为这块摄像头模块
**XCLK 要 FPGA 输出、SDA 是双向**，不能沿用全输入的命名。

### 综合结果（bd_video，无引脚约束）

| 资源 | 用量 | 占比 |
|---|---|---|
| Slice LUTs | 12,829 | 24.11% |
| Slice Registers | 15,807 | 14.86% |
| Block RAM Tile | 25.5 | 18.21% |
| DSPs | 61 | 27.73% |

> 含预处理链（gesture_preproc + 两个 DMA）与 SCCB/Clocking Wizard。
> DSP 偏高**不在** `thresh_stage`（它只占 2 个）——实测大头在 `morph_stage`（56 个）。
> 降 DSP 的尝试失败过，详见 `src_hls/README.md`。

---

## ⚠ XDC 的两个硬性限制（本项目实际踩到）

这两条**都不是语法笔误，是对 XDC 语言模型的误解**，且都只给
**CRITICAL WARNING 不给 ERROR** —— 流程照跑、约束静默失效。

### 限制 1：XDC 不支持 Tcl 的 `if` / `for` 等控制流

XDC 是 Tcl 的**受限子集**，只认约束命令。写了控制流会被解析器拒绝：

```
CRITICAL WARNING: [Designutils 20-1307]
  Command 'if' is not supported in the xdc constraint file.
```

**症状**：写了 `if {[llength [get_ports cam_pclk]] > 0} { create_clock ... }`
这种"保护式"写法，结果**整段约束一条都没生效** —— 因为 `if` 被拒后，
块内的命令也不会执行。

**正确做法**：用 `get_* -quiet`。它找不到对象时返回空列表而**不报错**，
约束命令作用在空列表上是安全的 no-op：

```tcl
create_clock -period 41.667 -name cam_pclk [get_ports -quiet cam_pclk]
```

### 限制 2：XDC 的执行早于 `link_design`

此时设计对象还没建立，`get_ports` / `get_clocks` 可能返回空，
依赖设计对象的约束会求值失败：

```
CRITICAL WARNING: [Vivado 12-4739] set_clock_groups:
  No valid object(s) found for '-group [get_clocks ...]'
```

**症状**：约束在综合日志里报 WARNING，看起来"没生效"。

**处理**：
1. Vivado 会对 `used_in implementation` 的文件在**实现阶段重读**一次，
   届时设计已 link，约束正确生效
2. 想减少综合阶段的噪音，用 `-of_objects [get_ports ...]` 形式，
   Vivado 会把它记为"待解析"，link 后自动补齐

**关键：不要只看综合日志的 WARNING 就下结论。**
用 `test_video_io_xdc.tcl` 实际检查——它跑完综合后执行
`get_clocks -quiet cam_pclk` 并读时钟交互报告，确认约束真的生效了。

### 验证用语

| 说法 | 含义 |
|---|---|
| "写完了 XDC" | 文件存在 —— **不代表生效** |
| "综合没报错" | 不代表约束应用了（很多是 CRITICAL WARNING） |
| "`get_clocks` 能查到 cam_pclk" | ✅ 这个才算生效 |

---

## bd_video 的数据通路

### 显示通路：摄像头原图直通，与预处理完全解耦

```
OV5640 ─DVP─► dvp_capture ─AXIS(16bit)─► VDMA S2MM ─► HP1 ─► DDR
                                                                │
                                                      (3 帧缓存)
                                                                │
   HDMI ◄─ TMDS ◄─ v_axi4s_vid_out ◄─AXIS◄─ VDMA MM2S ◄─ HP2 ──┘
                          ▲                     ▲
                       v_tc                  PS 配置(GP0)
```

### CNN 通路：从 DDR 取同一帧，处理成 96×96 灰度写回

```
DDR(640×480 RGB565)          ← 就是显示通路的那个帧缓存
      │
      │  dma_in (MM2S，读 614400 字节)
      ▼
gesture_preproc (HLS) ──► 96×96 uint8
      │
      │  dma_out (S2MM，写 9216 字节)
      ▼
DDR(96×96) ──► PS 侧 CNN 读这里
```

**地址映射**（validate 后自动分配）：
| 从设备 | 地址 |
|---|---|
| `vdma/S_AXI_LITE` | `0x4300_0000` |
| `v_tc/ctrl` | `0x43C0_0000` |
| `gesture_preproc_0/s_axi_control` | 见 Address Editor |
| `dma_in` / `dma_out` | 见 Address Editor |
| HP1/HP2/HP3 DDR | `0x0000_0000` (512M) |

驱动里用这些地址访问，**不要硬编码** —— 从生成的 `.hwh` 里读：

```bash
# .hwh 就在 BD 的 hw_handoff 目录下
vivado/gesture_system/gesture_system.gen/sources_1/bd/bd_video/hw_handoff/bd_video.hwh
```

⚠ `.hwh` 是几十万字节的 XML，**不要直接读进上下文**（会吃掉整个窗口）。
写个脚本提取，或用 PYNQ：`overlay.ip_dict` 直接给出所有 IP 的名字与地址
（`host/gesture_overlay.py` 就是这么做的）。

### ⚠ 为什么预处理从 DDR 取数，而不是从 dvp_capture 分叉

曾考虑用 `axis_broadcaster` 把 `dvp_capture` 的输出一分为二：
一路给 VDMA（显示），一路给 gesture_preproc（CNN）。

**那条路会反压崩溃**：broadcaster 要求所有输出都 ready 才收输入，
而 `gesture_preproc` 是 HLS 的 `ap_ctrl_hs` 模式 ——
**未 `ap_start` 时 `tready=0`**。CNN 不需要 30fps，它大部分时间空闲，
于是 broadcaster 反压 → VDMA 收不到数 → **HDMI 显示也一起卡死**。

从 DDR 分叉则完全解耦：
- 显示通路一字未动，CNN 通路出任何问题都不影响画面
- `gesture_preproc` 按 PS 的节奏跑（10 fps 都够），不必死磕 30fps 时序
- 同一份 DDR 数据在 PC 上也能拿来对拍 Python golden

代价是多一次 DDR 往返（614 KB/帧读），相对三个 HP 口的总带宽很小。

### 触发方式：帧触发 + 轮询

PS 侧流程：
```
检测新帧 → 先武装 dma_out(S2MM) → 再启动 dma_in(MM2S) → ap_start
        → 轮询 ap_done → 置 frame_ready
```

⚠ **顺序不能反**：S2MM 必须先武装，否则预处理输出的第一拍数据没有接收方。

### 资源占用

见上文「综合结果」表（2026-09-15 实测，含预处理链 + SCCB + Clocking Wizard）。

两个时钟域：`clk_fpga_0`（100 MHz）+ `cam_pclk`（24 MHz，来自摄像头）。

> **DSP 占 27.73% 偏高**，来源**不是** `thresh_stage`（它只占 2 个）——
> 实测大头是 `morph_stage` 的 56 个（占全设计 79%）
> （`sum / 9216` 被映射成 DSP 乘法器）。若后续资源紧张，
> 可改成"乘 1/9216 的定点倒数再右移"，能省下大部分 DSP。
> 详见 `src_hls/README.md`。

---

## ⚠ 踩过的 23 个坑（都在脚本注释里）

这些坑的共同特点：**错误信息与真正原因不在同一处**，或者**综合能过、上板才炸**。

> **第 20~23 条是上板实测补的** —— 本项目排查耗时最长的一组。
> 20/21 同族：**参数没生效、不报错**；22 是：**推理自洽但结论错**；
> 23 是：**改对了约束，反而暴露出被掩盖的物理冲突**。

### 1. `c_s_axis_s2mm_tdata_width` 是只读参数
报 `[BD 41-737] Cannot set the parameter ... It is read-only`。
它由 `c_m_axi_s2mm_data_width` 派生。

### 2. `c_mm2s_fsync` / `c_s2mm_fsync` 不存在
报 `[BD 41-1276] Parameter does not exist`。
⚠ **这类错误只给 CRITICAL WARNING，不报 ERROR** —— 极易被忽略，
然后你会以为参数已经设上了。

### 3. `v_tc` 的 `VIDEO_MODE` 只接受 720p/480p/1080p/Custom
写 `640x480` 报 `[IP_Flow 19-3461] out of the range`。**640×480 对应 "480p"**。

### 4. 实例名与变量名不一致
`create_bd_cell ... -vlnv ...:v_tc v_tc` 的实例名是 **v_tc**（不是变量名 `vtc`）。
连接时写 `vtc/ctrl` 报 `[BD 5-232] No interface pins matched`，
**错误信息里只提到引脚没匹配，不告诉你实例名写错了**。

### 5. `v_axi4s_vid_out` 没有 AXI 接口
它只有 `vid_io_out` / `video_in` / `vtiming_in` 三个接口，**纯流式，无需配置**。
给它接 AXI-Lite 会失败。用 `list_property_value` 或探测脚本确认接口列表，
不要凭 IP 名字猜。

### 6. 视频输出是**接口**不是散引脚
`vid_io_out` 是接口，没有 `vid_data`/`vid_hsync` 这些散引脚。
导出到外部用 `make_bd_intf_pins_external`，**不要手写 VLNV**
（`xilinx.com:interface:vid_io:1.0` 查不到，报 `[BD 41-52]`）。

### 7. AXI 互连的时钟和复位是**每端口一个**
```
漏连 → [BD 41-758] 时钟引脚未连接
漏连 → [BD 41-759] 输入引脚悬空，被 tie-off 到 0
```
**这是最危险的一类**：`S00_ARESETN` 悬空会让互连永远处于复位状态，
AXI 事务一条都过不去 —— 而**综合、实现、生成比特流全部会通过**。

只连 `ACLK`/`ARESETN` 远远不够，必须逐个连
`S00_ACLK` / `M00_ACLK` / `S00_ARESETN` / `M00_ARESETN`。

脚本里已加**逐个断言**，把这类问题消灭在 validate 之前。

### 8. 位宽不匹配只给 WARNING，但数据是错的
VDMA 出 16bit(RGB565)，`v_axi4s_vid_out` 默认 24bit，报：
```
[BD 41-2384] Width mismatch ... Only lower order bits will be connected
```
"能连上"但**颜色整体错位**。实测对应关系（`DATA_WIDTH=8`）：

| `C_S_AXIS_VIDEO_FORMAT` | tdata 位宽 |
|---|---|
| **0** | **16** ← RGB565 用这个 |
| 1 / 2 | 24 |
| 5 / 6 | 32 |

---

### 9. wrapper 有两份副本，综合用的是旧的那份

**症状**：改完 BD（比如端口 `cam_pclk` → `io_pclk`），综合报

```
[Synth 8-11365] named port connection 'cam_pclk' does not exist
[E:/.../sources_1/imports/hdl/bd_video_wrapper.v:52]
```

**报错指向 wrapper，根因却在别处** —— 工程里同时存在两份 wrapper：

| 路径 | 谁生成 |
|---|---|
| `.gen/sources_1/bd/bd_video/hdl/` | 每次 build BD 重新生成（**新**） |
| `.srcs/sources_1/imports/hdl/` | `make_wrapper -import` 时拷贝的（**旧**） |

**综合用 imports 那份。**

**处理**：`make_wrapper -force` **不保证覆盖 imports 里的拷贝**（实测无效）。
必须先手工删干净再重建：

```tcl
foreach w [get_files -quiet *bd_video_wrapper.v] { remove_files $w }
# 磁盘上也删（remove_files 只从工程移除）
file delete -force <imports>/bd_video_wrapper.v
make_wrapper -files [get_files bd_video.bd] -top
add_files -norecurse <gen>/bd_video_wrapper.v
set_property top bd_video_wrapper [current_fileset]
```

`test_video_io_xdc.tcl` 已内置这段逻辑。

### 10. `continue` 在 Vivado 的 Tcl 里会报错

```tcl
foreach f $files {
    if {[exists $f]} { continue }    # ✗ wrong # args: should be "continue"
    ...
}
```

Vivado 的 Tcl 解释器在部分上下文里对 `foreach` 内的 `continue` 报
`wrong # args`。**用嵌套 if 表达同样的"跳过"逻辑**：

```tcl
foreach f $files {
    if {![exists $f]} { ... }        # ✓
}
```

### 11. "文件加入列表"的守卫条件不能只看一个文件

```tcl
# ✗ 错误：一旦 dvp_capture.v 已存在，新增的 iobuf_wrap.v 永远加不进来
if {dvp_capture.v 不在工程} { add_files 所有文件 }

# ✓ 正确：逐个文件判断
foreach f $files {
    if {[llength [get_files -quiet "*/$f"]] == 0} { add_files ... }
}
```

症状是 `[BD 41-1690] Unable to resolve module-source: ov5640_regs`
—— 模块源找不到，但文件明明在磁盘上。

### 12. AXI 互连的主口数必须与实际占用一致

`ic_ctrl` 配 `NUM_MI=5`，但如果预处理链因缺 `IP_REPO` 被跳过，
M02/M03/M04 就**悬空**。`bd_video.tcl` 末尾的 AXI 接口检查会
直接 error 退出（**这是对的** —— 悬空 AXI 口综合能过、上板才炸）。

**跑任何 BD 脚本前先确认 `IP_REPO` 指向 HLS 导出目录**，
否则整个预处理链会被静默跳过。

---

### 13. `.gitignore` 的目录级忽略会让 `!` 放行失效

**症状**：想让 BD 的源（`.bd`/`.bda`/`ip/*.xci`）进版本控制，
写了目录级忽略再用 `!` 放行 —— **放行不生效**，BD 源还是被忽略。

**根因**（git 的硬规则）：

> **父目录一旦被忽略，就无法用 `!` 放行其中的任何文件。**

所以这种写法一定失败：

```gitignore
vivado/gesture_system/              # ✗ 忽略整个工程目录
!vivado/gesture_system/**/bd_video/ # ✗ 放行无效
```

**定位方法**（别猜，用这个）：

```bash
git check-ignore -v <路径>
```

它会告诉你**具体是哪一行规则**匹配的。本项目的实例：

```
.gitignore:15:vivado/gesture_system/    ← 指回目录级那条
```

**正确做法**：不忽略根目录，**逐条忽略可再生的子项**：

```gitignore
vivado/**/*.gen/
vivado/**/*.runs/
vivado/**/*.cache/
# ... 而不是 vivado/gesture_system/
```

> ⚠ 另一条相关陷阱：`vivado/*.gen/` 这种**单层** `*` 匹配不到
> `vivado/gesture_system/gesture_system.gen/`（两层）。必须用 `**`。
> 两者都会**静默失效** —— 写完规则以为忽略了，实际一个都没匹配上。
> **验规则要用 `git check-ignore` 实测，不要靠看。**

### 14. 约束里的对象名**不要靠猜** —— 报错在告诉你"找不到"

`video_io.xdc` 里曾有两处因为**对象名写错**而报错，都是抄来的：

| 约束 | 写的名字 | 实际是 | 报错 |
|---|---|---|---|
| `set_clock_groups` | `*processing_system7_0*FCLK_CLK0` | PS7 实例名 **`ps7`**，时钟 **`clk_fpga_0`** | `[12-5201]` + `[12-4739]` |
| `set_false_path` | `get_ports DDR_*` | 本 BD 的 PS7 **没把 DDR 引到顶层** | `[12-4739]` |

**这两条报错都是"报得对"的**：

```
[Vivado 12-5201] cannot set the clock group when only one non-empty
                 group remains
[Vivado 12-4739] No valid object(s) found for '-from [get_ports DDR_*]'
```

它们在说"你的约束里有个对象是空的"。**遇到别去改约束的写法，
要去查那个对象到底叫什么 / 存不存在。**

**查实方法**（综合后执行，别猜）：

```tcl
open_run synth_1
get_clocks      # 所有时钟名
get_ports       # 所有顶层端口名
get_cells -hier -filter {NAME =~ "*关键字*"}   # 层次单元名
```

本项目实测的时钟名：`clk_fpga_0` / `cam_pclk` /
`clk_out1_bd_video_clk_wiz_xclk_0` / `clkfbout_...`。

> ⚠ `DDR_*` 那条尤其值得注意：**它在原 Sobel 工程里同样是无效的**。
> 抄约束时不会连带抄来"这条在当前设计里成不成立"，
> 所以抄来的约束**必须逐条验证**。

### 15. `[Netlist 29-160]` 来自 Vivado 自动生成的 PS7 约束（**产物无害，但已降级为 INFO**）

综合日志里会刷 **101 条**：

```
CRITICAL WARNING: [Netlist 29-160] Cannot set property 'iostandard',
  because the property does not exist for objects of type 'pin'.
  [.../bd_video_ps7_0_5/bd_video_ps7_0.xdc:29]
```

**来源**：`bd_video_ps7_0.xdc` —— **Vivado 为 PS7 自动生成的**约束，
在约束 DDR_VRP / DDR_VRN 等 PS 引脚。

**为什么报**：这些引脚在**综合阶段还不存在**（PS 的 IO 由硬核管理，
不出现在 PL 的 netlist 里）。实现阶段会正常应用。

**结论**：不是本项目的问题，判据是看 `[文件路径]` ——
指向 `gesture_system.gen/.../ip/` 下的都是 Vivado 自动生成的，
指向 `vivado/constraints/` 才是我们的。

> ⚠ **但"忽略"≠"不管"**（2026-09-17 更正）：
> 这 100 条 CRITICAL WARNING 会**刷屏掩盖真问题** ——
> 本项目的 PS7 DDR 参数错误（出厂默认 `MT41J128M8` vs 板上
> `MT41K256M16`）当初就是被这批告警盖住的。
>
> 现在 `create_project.tcl` 里已加
> `set_msg_config -id {Netlist 29-160} -new_severity INFO`
> 把它们降为 INFO，让真问题浮出来。

### 16. **顶层被静默设成子模块**，整条 BD 未进综合

**现象**：实现阶段报
```
[DRC NSTD-1] 67 out of 67 logical ports use IOSTANDARD 'DEFAULT'
问题端口: cam_data[7:0], frame_cnt[15:0], line_cnt[15:0], aclk, pclk ...
```
这些是 **`dvp_capture` 的端口**，不是顶层 wrapper 的。
日志里是 `Command: synth_design -top dvp_capture`，
且 `synth_1` **只综合了 dvp_capture + async_fifo 两个模块**。

**根因**：BD 生成时会**重新生成 wrapper**，把 `make_wrapper` 的产物
从工程文件表里挤掉。Vivado 只好在剩余模块里**自动挑一个当顶层** ——
挑中了 `dvp_capture` 这个**子模块**。

**为什么难查**：**全流程静默通过** —— 综合、实现、出比特流都不报错，
只有 DRC 会冒出一堆莫名其妙的端口名。

**对策**（已加进 `create_project.tcl`）：
```tcl
set top_now [get_property top [current_fileset]]
if {$top_now ne "${BD_NAME}_wrapper"} {
    error "顶层设置失败：期望 ... 实际 '$top_now'"
}
```
**必须回读断言，不能靠"没报错就当对了"。**

---

### 17. `launch_runs` 对已失败的 run **不做任何事** → 重试是假的

**现象**：给 run 加"失败就重跑"的重试，日志显示重试了，但**同一秒就返回**：
```
>>> synth_1 进度 0% —— 第 1 次未完成
>>> 重试...
[21:38:34] Waiting for synth_1 to finish...
[21:38:34] synth_1 finished        ← 同一秒返回，根本没跑
```

**根因**：`launch_runs` 认为那个 run "已经跑过了"（只是结果是失败），
于是什么都不做。**光靠"再 launch 一次"是假重试。**

**对策**：重试前先 `reset_run`（已完成的子 run 不受影响，只有失败的需要重跑）：
```tcl
if {$attempt > 1} { catch {reset_run -quiet $run_name} }
```

**配套坑**：找失败子 run **不能用** `get_runs "${run_name}_*"` ——
run 叫 `synth_1`，子 run 却叫 `bd_video_dma_in_0_synth_1`，**glob 匹配不到**，
那段诊断会**静默失效**。要遍历全部 run 按后缀筛。

---

### 18. `get_timing_paths` 需要**已打开的设计**

**现象**：脚本跑到 "实现完成" 后突然中断：
```
>>> 实现完成
ERROR: [Common 17-53] User Exception: No open design.
```
**后果**：XSA 导出和资源报告**根本没执行到** ——
而 `write_bitstream completed successfully` 就在日志里，
容易误以为"都成功了"。

**根因**：`get_timing_paths` 写在 `open_run impl_1` **之前**。

**对策**：`open_run` 必须在前面；且时序查询本身加 `catch`
—— 它只是**报告**，不该有中断整个流程的能力。

**教训**：脚本"跑到最后一行"和"所有产物都生成了"是**两回事**。

---

### 19. `[Common 17-1257/354] Failed to create directory 'C'` —— 产物无害，**流程致命**

**现象**：OOC 综合日志里出现（每次打中的 run 都不一样，随机）：
```
ERROR: [Common 17-354] Could not open 'C' for writing.
ERROR: [Common 17-1257] Failed to create directory 'C'.
```
看起来像环境变量问题（某个变量为空被展开成裸盘符 `C`），**但不是**。

**根因**：一次性启动 16–22 个 OOC 综合，每个 Vivado 实例都要建一批
临时目录 —— **并发建目录竞争的瞬时失败**。重跑必然成功
（中招的 run 自己的 `.dcp` 其实照样生成了）。
已排除：`TEMP`/`TMP` 正常、无空环境变量、脚本里没有任何地方传过 `"C"`。

**⚠ 为什么不能当噪声忽略**：Vivado 会把这个瞬时失败**判定成 run 失败**：
```
ERROR: [Vivado 12-13638] Failed runs(s) : '<run 名>'
ERROR: [Common 17-39] 'wait_on_runs' failed due to earlier errors.
```
于是 `wait_on_run` **直接抛错返回**，脚本中断 —— **XSA 根本导不出来**。

**对策**：`create_project.tcl` 里已加带 `reset_run` 的重试（见坑 17）。
实测一次运行有 10 个子 run 中招，重试一次后全部恢复。

---

### 20. AXI DMA 传大块时**静默只传前 16 KB** —— 上板卡死，且全程不报错

> **来源**：2026-09-21 首次上板实测。**本项目排查耗时最长的一条**，
> 从"怀疑硬件坏"一路查到 BD 里一个从没设过的 IP 参数。
> 完整记录见 `../docs/board-test-log-2026-09-21.md` §三。

**现象**：上板跑「DMA → HLS 流水线 → DMA」，`ap_done` **永不置位**、10 秒超时。
但其它环节**看起来全是好的**：DDR 自检 4/4、6 个 IP 全认到、参数回读正确、
`CTRL=0x01`（`ap_start=1`、`ap_idle=0`）、**`dma_in` 的 `IOC_Irq` 照常置位**
（"传输完成"看起来完全正常）。**一条报错都没有。**

**根因**：例化 AXI DMA 时**没设 `C_SG_LENGTH_WIDTH`**，IP 用默认值 **14 位**
→ 单次传输上限 `2^14 - 1 = 16383` 字节。而输入帧是 **614400** 字节
（640×480×RGB565），**是上限的 37 倍**。

于是 DMA 只搬了前 16384 字节就置 `IOC_Irq`（**"完成"是真的，
只是它认为的"全部"只有 16 KB**）→ `crop_scale` 永远读不满输入
→ 整条 `DATAFLOW` 链停摆 → `dst` 不吐 → `ap_done` 永不置位。

**确诊证据**：写一组长度值再读回，规律是 `读回 = 写入 mod 16384`：

| 写入 LENGTH | 读回 | 写入 mod 16384 |
|---|---|---|
| 614400 | 8192 | 8192 ✅ |
| 123456 | 8768 | 8768 ✅ |
| 100000 | 1696 | 1696 ✅ |
| 65536 | 0 | 0 ✅ |

**修复**：两个 DMA 都显式设 `CONFIG.c_sg_length_width {24}`（= 16 MB 上限）。

```tcl
CONFIG.c_sg_length_width             {24} \
```

**为什么难查**：

1. **不报错** —— `IOC_Irq` 照常置位，"传输完成"看起来正常
2. **csim / cosim 查不出来** —— 仿真里没有真实的 AXI DMA 长度寄存器，
   这个限制**只在硬件上存在**，所以 `csim + csynth + cosim 全过`毫无帮助
3. **`C_SG_LENGTH_WIDTH` 名字有迷惑性** —— 看着只跟 Scatter-Gather 模式有关
   （我们是 `c_include_sg=0` 的 Direct 模式），容易认为"不用 SG 就不用设"。
   **实际上 Direct 模式也用它存传输长度。**

**验证命令**（改完 BD 必做）：

```bash
# 1) 确认参数真进了硬件描述（不是只写在 tcl 里）
unzip -p <XSA> bd_video.hwh | grep -i "SG_LENGTH_WIDTH"   # 期望 = 24
# 2) 上板后写一个超过 16383 的长度再读回，必须原样读回
```

> ⚠⚠ **修复前的比特流全部作废**：`v0.2-bitstream-ok` 与 `v0.3-board-ready`
> 两个 tag 的比特流都带这个 bug，**上板必坏**。
> 必须用 **`a4eabe6` 及之后**的比特流。

---

### 21. BD 里设的 IP 参数**没生效**，只给 WARNING —— XCLK 因此不出

> **来源**：2026-09-22。摄像头不出图的**最终根因**。
> **与坑 20 完全同族**：参数没进去、不报错、只在硬件上表现为怪现象。

**现象**：摄像头通路全死 —— XCLK 无输出 → SCCB 无 ACK（`cfg_error=1`）
→ 不出图 → VDMA 帧计数恒 1。**FPGA 侧没有任何报错。**

**根因**：`bd_video.tcl` 里给 Clocking Wizard 设 MMCM 参数，想避开
VCO 顶着 -1 速度等级上限：

```tcl
CONFIG.PRIMITIVE             {MMCM} \
CONFIG.MMCM_CLKFBOUT_MULT_F  {6.000} \      # ← 被静默丢弃
CONFIG.MMCM_DIVCLK_DIVIDE    {1} \          # ← 被静默丢弃
CONFIG.MMCM_CLKOUT0_DIVIDE_F {25.000} \     # ← 被静默丢弃
```

IP **根本不给改 M/O**，因为缺少开关：

```
WARNING: [IP_Flow 19-3374] An attempt to modify the value of
  disabled parameter 'MMCM_CLKFBOUT_MULT_F' from '50.250' to '6.000'
  has been ignored
```

**只给 WARNING，不给 ERROR。** 值直接丢弃，工具照用自己求解器的结果
（自动选了 VCO ≈ 1005 MHz，贴着上限）。

**⭐ 正确写法 —— 必须开 `OVERRIDE_MMCM`**：

```tcl
CONFIG.PRIMITIVE      {MMCM} \
CONFIG.OVERRIDE_MMCM  {true} \      # ← 缺了它，下面三行全是废的
CONFIG.MMCM_CLKFBOUT_MULT_F  {6.000} \
CONFIG.MMCM_DIVCLK_DIVIDE    {1} \
CONFIG.MMCM_CLKOUT0_DIVIDE_F {25.000} \
```

**⚠ 三个误导性的中间尝试（都失败，别重走）**：

| 试过 | 结果 |
|---|---|
| 把 MMCM_* 和 PRIMITIVE 放**同一个** `-dict` | ❌ 仍被忽略 |
| **拆成两步** `set_property`（先 PRIMITIVE 再 MMCM_*） | ❌ 仍被忽略 |
| 设 `USE_FREQ_SYNTH=false` | ⚠ 只解开 `DIVCLK_DIVIDE`，**M/O 仍被拒** |

**`OVERRIDE_MMCM` 在常规配置界面里不明显** —— 最后是靠打印
**完整属性表**才找到的：

```tcl
report_property -all $cw | grep -iE "OVERRIDE|MULT|DIVIDE"
```

**验证命令**（写完必跑）：

```tcl
# ① 回读断言 —— 别信"没报错就是设上了"
foreach {p want} {MMCM_CLKFBOUT_MULT_F 6.000 MMCM_DIVCLK_DIVIDE 1} {
    set got [get_property CONFIG.$p $cw]
    if {[expr {abs(double($got)-$want)}] > 0.001} { error "$p 未生效：$got" }
}
# ② 看日志有没有 19-3374
#    grep "19-3374" <build>.log     有输出 = 参数被丢了
```

> **一般化**：**"设了属性"和"属性生效了"是两件事。**
> 凡是 IP 参数，**设完必须回读断言**。本项目已被这类坑咬过三次：
> `C_SG_LENGTH_WIDTH`（坑 20）、`C_PROBE{N}_WIDTH`、以及本条。
> 它们的共同点：**只给 WARNING** —— 而 WARNING 在几千行的构建日志里
> 根本不会有人看。

---

### 22. 引脚映射靠"推理"而非**实物丝印** —— 14 个信号全错位

> **来源**：2026-09-22。摄像头不出图的**真正根因**。详见
> `skill/pitfalls/README.md` **P12**（同一件事的详细版）。

`video_io.xdc` 的映射是从**原理图画法***推理*出来的（"左列 12→7"的
非标准画法 → 选"镜像解读"）。**推理错了。**

**决定性证据是实物丝印**：

```
PMOD A 丝印： 1 NC  2 PCLK  3 HREF  4 SCL   5 GND  6 3V3
              7 NC  8 XCLK  9 VSYNC 10 SDA  11 GND 12 3V3
```

`5/6/11/12 = GND/VCC/GND/VCC` —— **正是 Pmod 规范的标准位置**，
而"镜像"解读会把电源推到 5/6/7/8，两者不相容。**模块用的是标准编号。**

结果 14 个信号全错，其中 6 个接到**空脚**上：

| 信号 | 应接 | 实际接到 |
|---|---|---|
| XCLK | U19 (pin 8) | Y18 = **pin 1 = NC** |
| PCLK | Y19 (pin 2) | U18 = **pin 7 = NC** |
| HREF | Y16 (pin 3) | U19 = pin 8 |
| VSYNC | W18 (pin 9) | Y19 = pin 2 |
| SCL | Y17 (pin 4) | W18 = pin 9 |
| SDA | W19 (pin 10) | Y16 = pin 3 |

**⭐ 交叉验证**：正确映射下未用的 **Y18/U18** 恰好对应模块 **pin1/pin7 = NC**。

> **教训**：**丝印 > 原理图推理**。丝印是实物标注，不需要推理；
> "从画法推断针号"引入了额外假设，**假设错了不会报错，只会让后续全错**。

---

### 23. 引脚位置与 FPGA **时钟专用脚不相交** → 布线失败

> **来源**：2026-09-22。**修好坑 22 之后才暴露出来**的独立问题。

**现象**（⚠ 注意：**综合能过，实现直接失败**）

```
ERROR: [Place 30-574] Poor placement for routing between an IO pin and BUFG
  Clock Rule: rule_gclkio_bufg   Status: FAILED
  Rule Description: An IOB driving a BUFG must use a CCIO in the
                    same half side (top/bottom) of chip as the BUFG
ERROR: [Place 30-99] Placer failed with error: 'IO Clock Placer failed'
```

**根因**：PCLK 在模块 **pin2 → ja[1] → Y19**（普通 IO），
但 Pmod A 上能驱动 BUFG 的 **CCIO 脚只有 U18/U19**，
而它们在模块上分别是 **pin7=NC** 和 **pin8=XCLK**。

**PCLK 的物理位置与 FPGA 的时钟脚不相交** —— 模块排布决定的固有冲突。

**⭐ 为什么原来能过**：旧的**错误**映射把 PCLK 放在 U18（正好 CCIO）。
**错误的接线反而掩盖了这个冲突** —— 改对之后才暴露。

**对策**（Vivado 报错信息里直接给了）：

```tcl
set_property CLOCK_DEDICATED_ROUTE FALSE [get_nets io_pclk_IBUF]
```

**可接受的理由**：PCLK 仅 **24 MHz**（周期 41.7 ns），
输入延时窗口 1.5 ns，非专用路径额外插入延时典型几 ns —— **余量几十倍**。
⚠ 但**必须靠实现后时序报告确认**（`cam_pclk` 组 WNS 为正）。

> **教训**：**修完一个 bug 要重跑到底**（综合→实现→比特流）。
> 本条的失败**恰恰出在实现阶段** —— 只跑到综合通过就会漏掉。
> 另外：**时钟输入必须落 CCIO 脚**是硬性物理要求，
> 选板卡/模块时要**先把时钟脚与 CCIO 对齐**再定接线。

---

## HDMI 通路（方案 B，未做）

### 现状：卡在哪

```
vdma/M_AXIS_MM2S → v_axi4s_vid_out → vid_io_out（并行视频）→ ✗ 断了
                         ▲
                    v_tc/vtiming_out
```

BD 导出的 22 个 `hdmi_vid_out_*` 是**并行视频**，而 HDMI 物理接口要
**TMDS 差分对**，两者数量对不上。缺两样：

| 缺什么 | 为什么必须 |
|---|---|
| **像素时钟** | `vid_io_out` 是**接口，不携带时钟**。TMDS 需要独立的时钟对 `hdmi_tx_clk_p/n` |
| **TMDS 编码器** | 并行 RGB + 同步信号 → 3 对差分串行（**8b/10b 编码**） |

### 临时措施（方案 A，已实施）

为了让比特流能生成：

| 文件 | 作用 |
|---|---|
| `constraints/video_io_hdmi_tmp.xdc` | 把 `NSTD-1` / `UCIO-1` 两条 DRC 降级 |
| `constraints/hdmi_drc_hook.tcl` | write_bitstream 的 **pre-hook**（关键） |

⚠ **为什么必须用 pre-hook**：在工程里直接 `set_property SEVERITY`
**无效** —— run 是独立进程。Vivado 的报错信息里明确说了要用 pre-hook。

⚠ **代价**：22 个 HDMI 端口在比特流里**悬空**，**上板时不要接 HDMI 线**。
CNN 通路与 HDMI 完全解耦，不受影响。

### 方案 B 的完整计划

| 步 | 产出 | 能否离线验证 |
|---|---|---|
| 1 | `rtl/tmds_encoder.v`（**纯 8b/10b 算法，不含原语**） | ✅ **iverilog 可测** |
| 2 | `rtl/hdmi_tx.v`（例化 OSERDESE2 + OBUFDS） | ❌ 只能 Vivado 综合验证 |
| 3 | MMCM 产生 **25.175 MHz** 像素时钟 | ✅ 综合后看频率 |
| 4 | BD 集成 + 引脚约束（模板已在 `video_io.xdc` 第四层） | ✅ validate + 实现 |
| 5 | 出比特流 | ✅ DRC 通过 |

**关键设计**：第 1 步把 8b/10b 算法**单独抽出来**，它不依赖任何
Xilinx 原语，**可以用 iverilog 喂已知像素、比对标准编码表**。
这样核心逻辑可信，剩下的只是原语接线。

**工作量**：① 30分 ② **2–4 小时（主要风险）** ③ 15分 ④ 15分 ⑤ 30分

⚠ **最大问题**：第 2 步用的 `OSERDESE2` / `OBUFDS` **全是 Xilinx 原语**，
iverilog 不认（与 `iobuf_wrap.v` 同样的问题）。**真正的验证只能靠板子**（现已上板），
而 HDMI 出问题的症状（花屏/黑屏/不识别）在"编码错/时序错/接线错"
之间很难区分。

> **建议**：等主线（摄像头→预处理→DDR）验证通之后再动 B。
> ✅ **主线前半段（预处理链）已通**（2026-09-21，11/11）；
> **剩下的卡点在摄像头 SCCB**（`cfg_error=1`，见「坑 20」下方的实测记录）。
> 那时可以边调边看，而不是盲写。

### 顺带要修的已有缺陷

`bd_video.tcl` 里 `H_ACTIVE` / `H_FRONT` / `V_ACTIVE` 等常量
**只在一个 `puts` 里用过，没写进任何 IP**。`v_tc` 只设了
`CONFIG.VIDEO_MODE {480p}`。做 B 时要一并补上显式时序参数：

```tcl
set_property -dict [list \
    CONFIG.H_ACTIVE {640} CONFIG.H_FRONT {16} \
    CONFIG.H_SYNC {96}    CONFIG.H_BACK  {48} \
    CONFIG.V_ACTIVE {480} CONFIG.V_FRONT {10} \
    CONFIG.V_SYNC {2}     CONFIG.V_BACK  {33} \
] $vtc
```

---

## 未做的部分（故意分步）

| 项 | 为什么不现在做 |
|---|---|
| **HDMI 的 TMDS 编码** | 见上节「HDMI 通路（方案 B）」。已用临时措施让比特流能生成 |
| **dvp_capture 的 AXI-Lite 控制口** | 当前 RTL 无寄存器接口，PS 读不到 `frame_cnt`/`stalled`。⚠ **2026-09-21 实测确认这是真障碍** —— 摄像头不通时，四个观测点（`sccb_0` 状态、`clk_wiz.locked`、`frame_cnt`、VDMA 帧计数）**软件一个都读不到**，只能靠 ILA。见 `../docs/board-test-log-2026-09-21.md` §5.1 |
| ~~`rtl/ov5640_regs.v` 的真实寄存器表~~ | ✅ **2026-09-17 已换为真表**（250 条，正点原子来源，固化 640×480 RGB565）。✅ **2026-09-21 上板已确认表发出了**（ILA 抓到 `sccb_0/cfg_error=1` → 事务有发出但无 ACK，问题在 XCLK/接线，**不是表内容**）。✅ BD 里 `sccb_0` 的 `N_REGS=250` 也已确认生效 |

## 已完成的验证

| 项 | 状态 |
|---|---|
| BD 构建 + `validate_bd_design` | ✅ 无 CRITICAL WARNING |
| 时钟/复位/AXI 接口断言 | ✅ 全部通过 |
| **综合** | ✅ 通过 |
| **实现（place & route）** | ✅ **完成** |
| **时序收敛** | ✅ **WNS +0.265 ns**，TNS 0，0 个失败端点。⚠ **逐次波动大**：同一设计三次实现实测为 +0.873 / +1.177 / **+0.265** ns（布线是随机的）。⚠⚠ **且这 0.265 属于 AMD `v_tc` IP 内部，不是本设计的余量** —— 见下方「时序余量」 |
| **DRC** | ✅ 仅 Advisory（VDMA 内部 FIFO），无实质违规 |
| **比特流** | ✅ **已生成**（4.0 MB，用 HDMI 临时豁免） |
| **XSA** | ✅ 已导出（755 KB） |
| 功耗 | ✅ 2.009 W，结温 48.2°C |
| `video_io.xdc` 生效 | ✅ `cam_pclk` 与异步时钟组已确认 |
| **板级实测 ②③** | ✅✅ **已做（2026-09-21）**：DDR 自检 4/4 + 预处理链 **11/11 全过**，单帧 0.005 s |
| **板级实测 ④~⑧** | ❌ 摄像头通路卡在 SCCB（`cfg_error=1`），未区分 XCLK / 接线 |
| ⚠ **比特流版本** | 上表各项（资源/时序/功耗）是**修复前**那次实现的结果；`C_SG_LENGTH_WIDTH` 修复后已重新生成，**资源/时序基本不变**（只多了 10 位长度寄存器） |

> ⚠⚠ **上板必须用 `a4eabe6` 及之后的比特流** —— 见「坑 20」。
> 本表列的资源/时序数字来自那次修复**之前**的实现，两者差异可忽略，
> 但**不能拿修复前的 `.bit` 上板**。

### 最终资源占用（实现后实测）

| 资源 | 用量 | 占比 |
|---|---|---|
| Slice LUTs | **11,255** | 21.16% |
| Slice Registers | 14,810 | 13.92% |
| Block RAM Tile | 25.5 | 18.21% |
| DSPs | 61 | 27.73% |
| Bonded IOB | 35 | 28.00% |

> ⚠ **Slice LUTs 那一行曾被写错**（2026-09-18 核对 `utilization.rpt` 发现）：
> 原写作 `11,177 / 21.01%` —— `11,177` 其实是**同一个表里 Slice Registers
> 下面那个 HLS 内部口径的 FF 数**，被误当成了 LUT。
> 而 `21.01%` 正好是 `11177/53200`，所以**两个数互相"印证"，
> 看起来很像真的**。实际 LUT 是 **11,255（21.16%）**。
>
> **教训：同一张表里的数串了行，且占比自洽，肉眼查不出来 ——
> 必须回原始报告逐字比。**

### 时序余量

```
cam_pclk    WNS +35.020 ns   ← 24 MHz，余量巨大（也不是本设计，是 dvp_capture 的 FIFO 网）
clk_fpga_0  WNS  +0.265 ns   ← 100 MHz，逐次波动 0.265~1.177
```

> ⚠⚠ **更正（2026-09-18）**：`clk_fpga_0` 这 0.265 ns **不属于本设计**。
> 查布线报告的 10 条最差 setup 路径，**全部属于 AMD `bd_video_v_tc_0`
> （视频时序控制器）IP 内部**：
>
> ```
> Slack (MET) :   0.265ns
>   Source / Target :  .../bd_video_v_tc_0/<hidden>/<hidden>/<hidden>
>   Logic Levels    :  1  (LUT3=1)
>   Data Path Delay :  9.764ns  (logic 0.642ns (6.6%)  route 9.122ns (93.4%))
>   net (fo=433, routed)  9.122ns     ← 扇出 433 的高扇出网
>   Timing Exception:  MaxDelay Path 10.000ns -datapath_only
> ```
>
> **逻辑只占 0.64 ns、布线占 9.12 ns** → 是布线主导，不是逻辑深度；
> 两端都在加密 IP 内部 → **改我们的 RTL/HLS 对它零影响**。
>
> **本项目自己的余量**：HLS csynth 估 `morph_stage` 腐蚀流水线
> 可跑 **143.31 MHz**（目标 100 MHz）→ **余量 +43%**。
>
> 所以：**加逻辑不必因这个数畏手畏脚**，但仍应复查实现后报告。
> 详见 [`../report/design.md` §4.5](../report/design.md)。

**复现方式**（自己看一遍，别只信这里的结论）：

```bash
# 按 setup/hold 与时钟组分别统计布线后的最差路径
python - <<'EOF'
import re
t=open('vivado/gesture_system/gesture_system.runs/impl_1/'
       'bd_video_wrapper_timing_summary_routed.rpt',encoding='utf-8',errors='replace').read()
for b in re.split(r'(?=Slack \((?:MET|VIOLATED)\))', t):
    m=re.match(r'Slack \((MET|VIOLATED)\)\s*:\s*(-?[\d.]+)ns', b)
    if not m: continue
    g=re.search(r'Path Group:\s*(\S+)', b); pt=re.search(r'Path Type:\s*(\w+)', b)
    if g and g.group(1)=='clk_fpga_0' and pt and pt.group(1).startswith('Setup'):
        print(m.group(2), re.search(r'Source:\s*(\S+)', b).group(1)[:70])
EOF
```

### 产物位置

```
vivado/gesture_system/gesture_system.xsa                          ← 755 KB
vivado/gesture_system/gesture_system.runs/impl_1/bd_video_wrapper.bit  ← 4.0 MB
```

