# 上板实测记录 — 2026-09-22

> **状态**：查出并修复了**两个根因**（MMCM 参数、引脚映射）；
> **摄像头路仍未打通**；但**找到了 PL 算法的一个真 bug**（见 §十）。
>
> 本文与 `board-test-log-2026-09-21.md` 的分工：那份是首次上板，这份是**第二天**。
> 两份都按时间顺序保留**完整排查过程（含走错的弯路）**。

---

## 一、结论速览

| 项 | 结果 |
|---|---|
| ②③ 预处理链 | ✅ **依旧通过**（本次未重测，昨日 11/11） |
| **XCLK 无输出** | ✅ **根因找到并修复** —— 见 §二 |
| **引脚映射** | ✅ **发现全错并更正** —— 见 §三 |
| **CCIO 冲突** | ✅ 发现并用 override 解决 —— 见 §四 |
| **摄像头出图** | ❌ **仍未通** —— 帧计数 0，见 §六 |
| **PL 算法 bug** | ✅ **发现 `crop_scale` 整数块平均缺陷** —— 见 §十（**明天修**） |
| 蓝屏（2 次） | ⚠ 未解决，见 §七 |

**三个提交物**（都已进仓库）：

| 问题 | 修复 |
|---|---|
| MMCM 参数被静默忽略 | `bd_video.tcl` 加 `CONFIG.OVERRIDE_MMCM {true}` |
| 引脚映射"镜像"推导错误 | `video_io.xdc` 按模块丝印重写 |
| PCLK 与 CCIO 不相交 | `video_io.xdc` 加 `CLOCK_DEDICATED_ROUTE FALSE` |

---

## 二、根因一：MMCM 参数被**静默忽略**（XCLK 因此不出）

### 2.1 现象

摄像头通路全死。ILA 抓到：

```
io_xclk          → 全程恒 1，无翻转        ← FPGA 侧就没出时钟
io_pclk          → 恒 1
sccb_0/cfg_error → 1
frame_cnt        → 恒 1
```

### 2.2 根因

`bd_video.tcl` 给 Clocking Wizard 设 MMCM 参数想避开 VCO 上限：

```tcl
CONFIG.PRIMITIVE             {MMCM} \
CONFIG.MMCM_CLKFBOUT_MULT_F  {6.000} \      # ← 被静默丢弃
CONFIG.MMCM_DIVCLK_DIVIDE    {1} \          # ← 被静默丢弃
CONFIG.MMCM_CLKOUT0_DIVIDE_F {25.000} \     # ← 被静默丢弃
```

```
WARNING: [IP_Flow 19-3374] An attempt to modify the value of
  disabled parameter 'MMCM_CLKFBOUT_MULT_F' from '50.250' to '6.000'
  has been ignored
```

**只给 WARNING，不给 ERROR。** 工具照用自己求解器的结果
（自选 VCO ≈ 1005 MHz，贴着 -1 速度等级上限）。

### 2.3 ⭐ 解药：`CONFIG.OVERRIDE_MMCM {true}`

**三个误导性的中间尝试（都失败，别重走）**：

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

**正确写法**：

```tcl
# 第一步：开 OVERRIDE_MMCM
set_property -dict [list ... CONFIG.PRIMITIVE {MMCM} \
                        CONFIG.OVERRIDE_MMCM {true}] $cw
# 第二步：这时 MMCM_* 才可写
set_property -dict [list CONFIG.MMCM_CLKFBOUT_MULT_F {6.000} \
                        CONFIG.MMCM_DIVCLK_DIVIDE {1} \
                        CONFIG.MMCM_CLKOUT0_DIVIDE_F {25.000}] $cw
# 第三步：回读断言（必须！）
```

新参数：`M=6 / D=1 / O=25` → **VCO = 600 MHz**（区间正中）

### 2.4 验证：XCLK 活了

修复后 ILA `probe6`（`clk_out1`）抓到**规整方波** ✅

---

## 三、根因二：引脚映射按"镜像"推导 —— **14 个信号全错**

### 3.1 现象

修好 XCLK 后，**XCLK 有波形了，但 `io_pclk` 依旧恒 1**。

### 3.2 ⭐ 决定性证据：**读模块丝印**

`video_io.xdc` 的映射是从**原理图画法***推理*出来的
（"左列 12→7"的非标准画法 → 选"镜像解读"）。**推理错了。**

**实物丝印**（用户直接读的连接器标注）：

```
PMOD A 丝印： 1 NC   2 PCLK  3 HREF  4 SCL   5 GND  6 3V3
              7 NC   8 XCLK  9 VSYNC 10 SDA  11 GND 12 3V3

PMOD B 丝印： 1 D7   2 D5    3 D3    4 D1    5 GND  6 3V3
              7 D6   8 D4    9 D2   10 D0   11 GND 12 3V3
```

**`5/6/11/12 = GND/VCC/GND/VCC` —— 正是 Pmod 规范的标准位置。**
而"镜像"解读会把电源推到 5/6/7/8 —— **两者不相容**。
丝印符合标准 ⇒ **模块用的是标准编号**。

### 3.3 错误映射造成的结果

| 信号 | 应接 | 旧映射接到 | 后果 |
|---|---|---|---|
| XCLK | U19 (pin 8) | Y18 = **pin 1 = NC** | 摄像头无主时钟 |
| PCLK | Y19 (pin 2) | U18 = **pin 7 = NC** | 读悬空脚 → 恒 1 |
| HREF | Y16 (pin 3) | U19 = pin 8 = XCLK | |
| VSYNC | W18 (pin 9) | Y19 = pin 2 = PCLK | |
| SCL | Y17 (pin 4) | W18 = pin 9 = VSYNC | |
| SDA | W19 (pin 10) | Y16 = pin 3 = HREF | |

**⭐ 交叉验证**：正确映射下未用的 **Y18/U18**，恰好对应模块
**pin1/pin7 = NC**。该空的空着、该接的接上。

### 3.4 为什么难查

1. **推理链自洽** —— 图片原理图 → 镜像解读 → 选 ja[] → 得引脚，
   每一步都能自圆其说，**没有任何一步会报错**
2. **"XCLK 有输出"给了强烈反向暗示** —— 既然 FPGA 在发时钟，
   直觉上不会怀疑"接错线"
3. **"镜像假设"当初配了个看似有力的理由**（"只有它能让电源脚对齐"）——
   而实测证明**标准编号下电源脚本来就对齐**
4. 文档写着"必须万用表复核"，**而那一步始终没做** —— 已知风险拖了一个月

---

## 四、根因三：引脚位置与 **CCIO 不相交** → 布线失败

修正映射后重新综合，**实现阶段直接失败**：

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

**可接受的理由（有时序报告背书）**：

```
cam_pclk   Setup  34.794 ns  MET     ← 余量几十倍
cam_pclk   Hold    0.086 ns  MET
clk_fpga_0 Setup   0.517 ns  MET
```

PCLK 仅 24 MHz（周期 41.7 ns），输入延时窗口 1.5 ns，余量充足。

---

## 五、中间产物与验证链

| 版本 | MD5 | 内容 |
|---|---|---|
| `gesture_system.bit` | `7d177b3f` | 原始（引脚错 + MMCM 错） |
| `gesture_ila.bit` | `4f5399da` | 早期 ILA 版 |
| `gesture_mmcm.bit` | `ab9ad369` | ✅ MMCM 修了，引脚仍错 |
| **`gesture_pins.bit`** | **`db35cc1a`** | ✅✅ **引脚 + MMCM 都修了（当前最新）** |

> ⚠ **四个 `.bit` 文件大小全部相同（4,045,692 字节）** ——
> `.bit` 是整片器件镜像，大小恒定，**只有 MD5 能区分版本**。
> 这条已第二次踩到，见 `board-test-log-2026-09-21.md` 附录。

---

## 六、摄像头仍未通（**当前卡点**）

引脚修正后的实测（`camera_probe.py --bit gesture_pins.bit`）：

```
[7] 结果
    帧计数：基线 1 → 结束 1，本次运行**变化 0 次**
    停在 buf2，非零字节 0/614400 (0.0%)
```

**和修复前一模一样。** 说明引脚修正**没有解决问题**。

### 6.1 已排除

| 假设 | 排除依据 |
|---|---|
| 插接方向 | 用户已检查，J2→Pmod A、J3→Pmod B 无误 |
| 模块未插到底 | 已断电重插，仍全 0 |
| ILA 探针没接上 | `.bd` 网络表证明 `probe6 → clk_out1` 真连着 |
| 复位路径 | 时钟域匹配、极性正确 |

### 6.2 待查（下一步）

| 可能 | 判别方法 |
|---|---|
| **丝印编号排布理解错** | 看 **5(GND) 和 11(GND) 是不是上下相邻** —— 若不是，说明是另一种排法，映射要重算 |
| **信号没到达模块** | 逻辑分析仪量**模块 XCLK 测试点**（现在引脚修了，量才有意义） |
| **模块没供电** | 逻辑分析仪当直流表量**模块 3V3 测试点**（恒 1 = 有电） |
| 模块本身坏 | 以上都排除后才考虑 |

> **判读要点**：上次量 XCLK 是平线**是正确的**（那时信号被送到空脚）。
> 现在引脚改了，**再量才有诊断价值**。

---

## 七、⚠ 两次蓝屏（**未解决**）

| 时间 | 当时操作 | BugCheck |
|---|---|---|
| 21:04 | Vivado **综合** | `0x139 (0xa, 0, 0, ...)` |
| 22:14 | Hardware Manager **Auto Connect** | `0x139 (0xa, 0, 0, ...)` |

**分析**：

- `0x139` = `KERNEL_SECURITY_CHECK_FAILURE`
- `Param1 = 0xA` = **Control Flow Guard 间接调用检查失败**
  （内核态非法的间接 CALL/JMP）
- **不是显卡故障** —— `Display`/`DxgKrnl`/`nvlddmkm` 日志**零条**
- **不是硬件故障** —— WHEA 日志**零条**
- 两次参数完全一致 → **同一驱动、同一条代码路径**

**两次都在 Vivado 与硬件交互时**，第二次尤其明确（Auto Connect 会扫描
JTAG 链、加载 FTDI 驱动）。

**对策**：
1. **短期**：绕开 JTAG —— 用 PYNQ Python 验证（本次已这么做）
2. **中期**：装 WinDbg 分析 dump 的调用栈，定位具体驱动
3. ⚠ **不要在没搞清楚前反复试 `Auto Connect`**

---

## 八、附带修掉的问题

| # | 问题 | 修复 |
|---|---|---|
| 1 | `camera_probe.py` 硬编码加载旧 `.bit` | 加 `--bit` 参数，并打印实际加载路径 |
| 2 | `camera_probe.py` 的 UTF-8 包装在 Jupyter 崩溃（`OutStream has no attribute 'buffer'`） | 加 `hasattr` 守卫 |
| 3 | Vivado 调试时误建 `.srcs/` 空目录 | 已删 |

> ⚠ **第 2 条在其余 7 个脚本里同样存在**（`bringup_check.py`、
> `len_loopback.py` 等，全是无守卫的 `sys.stdout.buffer`）——
> **在 Jupyter 里跑都会崩**。待统一修复。

---

## 十、⭐ 发现的 PL 算法 bug：`crop_scale` 整数块平均缺陷（**明天修**）

> **本节是今天最有价值的产出。** 它和摄像头无关 —— 是**喂了真实图像之后**
> 才暴露出来的 HLS 实现缺陷。之前用 `fill_test_pattern()` 的合成图从没触发。

### 10.1 怎么发现的

**背景**：摄像头没通，于是改用**方案 A（静态图喂进 DDR）**验证 PL 链路 ——
这条路**不需要摄像头**，直接把图片写进 DDR 输入缓冲。

**结果**：链路跑通了（单帧 0.005 s、96×96 输出、动态范围 0~255），
但**和 golden 参考对不上**：

| 输入 | 板子非零 | golden 非零 | 比值 |
|---|---|---|---|
| 真实照片 | 3849 | 2414 | 1.6× |
| **合成图**（黑底+白块） | **2681** | **640** | **4.2×** |

**关键一步：用合成图定位。**

真实照片结构复杂，差异图看不出规律。换成"黑底 + 白块"（答案已知）后，
两者的**形状差异一目了然**：

```
GOLDEN（正确）          板子输出
    ┌─────────┐        ┌─────────────────────────┐
    │  (空)   │        │█████████████████████████│
    └─────────┘        │█████████████████████████│
   矩形【轮廓】         └─────────────────────────┘
                      实心大块
```

### 10.2 根因：整数块平均**填不满 96**

`src_hls/gesture_preproc.cpp` 的 `crop_scale`：

```c
int step_x = (roi_w + G::OW - 1) / G::OW;   // (320 + 96 - 1) / 96 = 4
int step_y = (roi_h + G::OH - 1) / G::OH;   // 4
...
for (int x = 0; x < width; x++) {           // 遍历 640 列
    if (in_roi) {
        acc += gray; cx++;
        if (cx == step_x) {                  // 每 4 列输出一个
            ...
            if (cy == step_y) ox[n_out++] = acc / (step_x * step_y);
        }
    }
}
while (n_out < G::OPIX) ox[n_out++] = 0;    // ← 补零兜底
```

**`roi_w = 320` 能被 `step_x = 4` 整除**，于是：

```
实际输出 = 320 / 4 = 80 个/行      但 OW = 96，需要 96 个
→ 只有 80 × 80 = 6400 个被填充
→ 9216 - 6400 = 2816 个补零
```

**实测非零 2681 ≈ 理论 2816** —— 吻合（差额来自阈值处理）。

**逐行非零统计也印证**：大多数行正好 **94** 个非零（= 96 − 2），
这是"前 80 个有内容、后面补零"在行方向上的表现。

### 10.3 为什么一直没暴露

1. **`fill_test_pattern()` 的测试图恰好没触发** —— 这是关键：
   此前的验证**从没喂过真实图像**
2. **csim 用的是"同一套 C++ golden"** —— 如果那个 golden 也没覆盖
   `roi_w` 整除边界，两边会**一起错**（pitfalls **P6 是同一类**）
3. **Python golden 走的是浮点比例缩放**，与 HLS 的整数块平均
   **语义本就不同** —— 直到对拍才显形

### 10.4 修复方案（明天做）

**方案 A：改 HLS —— 用"按比例分配"代替固定块平均**（治本）

```c
// 第 j 个输出像素覆盖源图区间 [j*roi_w/96, (j+1)*roi_w/96)
// 这样无论 roi_w 是多少，都**恰好产生 96 个输出**
```

**为什么必须改 HLS 而不是改 golden**：

- **接口契约是和 CNN 侧约定的"96×96 全填充"** ——
  现在只有 80×80 有内容，**CNN 会拿到带黑边的图**
- 改 golden 去迁就 bug，等于**让参考实现跟着错**

**工作量**：改 `crop_scale`（~20 行）→ `vitis-run --mode hls`（csim 验证）
→ 重新综合（约 40 分钟）

### 10.5 附带确认的几件事

| 项 | 结论 |
|---|---|
| 寄存器偏移 | ✅ 与 `xgesture_preproc_hw.h` 逐条一致 |
| 参数写入 | ✅ `gain=256` / `thresh_offset=-8` 三处（golden/overlay/HLS）一致 |
| 输入文件完整性 | ✅ MD5 逐字节一致（排除"传输出错"） |
| 版本身份 | ✅ MD5 校验，板子上跑的确实是引脚修正版 |
| 不是位移/翻转 | ✅ 各种翻转的相关系数都 ≤ 0.24 |

---

## 九、下次接着做

**优先级 1（明天做）—— 修 `crop_scale`，见 §十**

按"按比例分配"重写缩放逻辑 → csim → 重新综合 → 重新对拍。
**这是唯一已知的、确定要修的代码 bug。**

**优先级 2 —— 摄像头（不阻塞主线）**

方案 A 已让主线跑通，摄像头可以并行推进：

1. **确认丝印排布** —— 5/11 是否上下相邻（决定映射对不对）
2. **量模块 XCLK 测试点**（逻辑分析仪，避开 JTAG）
3. **量模块 3V3**（同上，逻辑分析仪当直流表用）
4. 若以上都正常 → 才是模块本身的问题
5. **蓝屏**：要再用 ILA 就得先解决它

> ⚠ **摄像头不再是关键路径** —— 方案 A（静态图）已能满足赛题
> "板上跑通 + 实测输出 + 与基线对比"三条硬指标。
> 摄像头是**加分项**，不是阻塞项。

---

## 附：本次环境

| 项 | 值 |
|---|---|
| 板卡 | PYNQ-Z2 (XC7Z020-1CLG400C) |
| 当前比特流 | `gesture_pins.bit`（`db35cc1a...`） |
| 构建命令 | `vivado -mode batch -source vivado/create_project.tcl` |
| ⚠ 注意 | **脚本路径要用绝对路径** —— 工作目录被重置过，相对路径找不到 |
| 时序 | WNS +0.517 ns，cam_pclk 组余量 34.8 ns |
