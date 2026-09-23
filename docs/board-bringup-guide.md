# 上板测试指南 — 手势识别系统

> 面向：`(本仓库根目录)`，板卡 **PYNQ-Z2**（XC7Z020）
> 工具：Vivado / Vitis **2025.2**
> 编写：2026-09-17
>
> **本文只写"上板"这一段**，且**只写能验证的东西**。
> 全文按「先排除不可逆的错 → 再逐级放行」排序，
> 每一步都给出**判定标准**和**失败对策**。
>
> ⚠ 本文的结论分两类，已分别标注：
> - **【已核实】**：来自本机文件/日志，可复查
> - **【未验证】**：推理或待实测，**不要当成结论用**

---

### 本文用到的四个环境（**每段代码前都标了在哪跑**）

上板这件事最容易卡住的地方，不是命令本身，而是**不知道该在哪儿敲**。
所以本文在**每段代码前面**都加了一个标记：

```
【终端：PC · Git Bash】              ← 在你的开发机上
【终端：PYNQ · Jupyter 或串口】       ← 在板子上的 Linux 里
【终端：串口 · MobaXterm】            ← 串口终端（看启动日志用）
【终端：PC · 串口】                   ← PC 上的串口终端（仓库自带脚本）
【终端：仪器 · 万用表】               ← 手持仪器，不是电脑
```

五个环境分别是：

| 标记 | 是什么 | 怎么进去 |
|---|---|---|
| **PC · Git Bash** | 你的开发机（有 Vivado/Vitis、仓库源码） | 右键 → Git Bash Here；或用 MobaXterm 的 Local terminal |
| **PYNQ · Jupyter** | 板子上的 Python | 浏览器开 `http://<板子IP>:9090` |
| **PYNQ · 串口** | 板子上的命令行（和 Jupyter 是同一个 Linux） | 见 §3.1 |
| **PC · 串口** | PC 上连板卡串口的终端（就是 §3.1 那个口） | `python host/pynq_serial.py <COM口>` |
| **仪器** | 万用表 / 示波器 | 手持操作，不需要电脑 |

> ⚠ **PC 和 PYNQ 上都有 Python，但装的东西完全不同**：
> PC 上**没有 `pynq` 模块**（`import pynq` 会失败，这是正常的）；
> PYNQ 上**没有 Vivado**。
> 在错的那一侧跑，报错是 `ModuleNotFoundError` 或 `command not found` ——
> 看着像环境坏了，其实只是跑错了地方。**这就是这些标记存在的理由。**


---

## 0. 先说结论：**已上板；静态图通路与 golden 逐字节一致；摄像头仍卡在 SCCB**

> ✅ **2026-09-23 更新**：**方案 A（静态图直接喂 DDR，绕开摄像头）已充分验证** ——
> 三次不同输入 / 三种 ROI，板上输出与 golden **逐字节一致**（各 0/9216）。
> 见 [`board-test-log-2026-09-23.md`](board-test-log-2026-09-23.md) §9。
>
> **摄像头不再是关键路径**（赛题 §3.3 未要求必须用 DVP 摄像头）。
> 下面的 B5 仍待查，但**不阻塞提交物**。

**摄像头选型已定：PMOD-CAMERA v1.0（MUSE LAB），直插 Pmod A + Pmod B。**
所以 BD 里的 `clk_wiz_xclk` 和 `io_xclk` 约束**都要保留**（见 §1.1）。

| # | 项 | 状态 |
|---|---|---|
| ~~B1~~ | 摄像头选型 | ✅ **已定：PMOD-CAMERA v1.0（2026-09-17）** |
| ~~B2~~ | `io_xclk` 引脚约束 | ✅ **不缺** —— `video_io.xdc:183` 有 `PACKAGE_PIN Y18`（见 §1.2 的更正说明） |
| ~~B3~~ | `rtl/ov5640_regs.v` 是**占位寄存器表** | ✅ **2026-09-17 已换真表**；✅ 2026-09-21 上板确认**事务有发出**（见下） |
| ~~B4~~ | PS7 DDR 参数 | ✅ **2026-09-17 已修复并重跑验证**（见 §1.4） |
| **B5** | **摄像头不出图** | ⚠ **唯一未通项，但已不阻塞**（方案 A 已满足赛题硬指标）—— ILA 抓到 `sccb_0/cfg_error=1`（配置无 ACK），**未区分 XCLK / 接线**（见 §5.3.1） |

> **B3 已消掉** —— 换真表后表内容不再是嫌疑；上板进一步确认
> **事务发得出去**，只是收不到 ACK。**现在的阻塞是 B5。**
>
> **§5.2（只跑预处理链）已经做掉了** —— 2026-09-21 实测 **11/11 全过**，
> 单帧 ≈5 ms。完整过程见 [`board-test-log-2026-09-21.md`](board-test-log-2026-09-21.md)。
>
> 还有一件"上电前必做但**至今没做**"的事（不是代码问题）：
> - **引脚万用表复核**（§2.2）—— PMOD-CAMERA 的原理图是"镜像编号"，
>   XDC 里的映射是**推理**的，从未实测。插错方向会让 3V3 与 GND 反接。**会烧板。**
>   ⚠ 它同时也是 B5 的**候选原因之一**（接线错 = SCCB 收不到 ACK）。
> - 短路检查（§2.1）—— ✅ 已做

---

## 1. 前置条件（上电前必须确认的事项）

### 1.1 ~~B1~~ — 摄像头模块（**已定：PMOD-CAMERA v1.0** ✅）

**2026-09-17 确认：用 MUSE LAB PMOD-CAMERA v1.0，直插 Pmod A + Pmod B。**
无需转接板、无需焊接。

| | 值 |
|---|---|
| 模块 | **PMOD-CAMERA v1.0**（TMALL 购入），公头 |
| 接线 | J2 → **Pmod A**（6 根），J3 → **Pmod B**（8 根数据） |
| 传感器 | OV5640 |
| 晶振 | **无** —— XCLK 必须由 PL 提供 24 MHz |
| 约束 | ✅ `video_io.xdc` 第三层就是按它写的，**可直接用** |

> ### ⚠ 为什么这条重要：文档曾经把它写错了
>
> `architecture-contract.md` §二 曾经整节按 **ATK-OV5640（正点原子）** 写，
> 理由是"板载 24 MHz 有源晶振，不需要 PL 提供 XCLK"（2026-09-15 的决策）。
> **那个决策现在被推翻了** —— 回到 PMOD-CAMERA。
>
> 这个反复导致了两处连锁错误，都已修正：
> - 契约文档 §二/§四 的"14 → 13 根线"、"Arduino 连接器"已改回 Pmod A+B
> - 本指南初版据此误判 `io_xclk` "没有约束"（见 §1.2 的更正）
>
> **教训**：换模块会连锁影响**接线数、XCLK 方向、约束文件、BD 模块**四处，
> 每次换都要把这四处重新对一遍，不能只改选型那一句。

**两个连接器的信号分配**：

```
J2 → Pmod A（6 根）： XMCLK / DVP_VSYNC / I2C_SDA / DVP_PCLK / DVP_HREF / I2C_SCL
                      （另有 2 个 NC 脚：ja[3]=Y17, ja[7]=W19）
J3 → Pmod B（8 根）： D6 D4 D2 D0 D7 D5 D3 D1  ← 数据线是**交错**的，不是顺序
```

> ⚠ **Pmod A 与 Raspberry Pi 口 8 根线并联，二者只能用其一**
> （Y18 Y19 Y16 Y17 U18 U19 W18 W19）。本方案用 Pmod A，
> **RPi 口同时废掉** —— 本项目不用 RPi，可接受。

---

### 1.2 ~~B2~~ — `io_xclk` 约束（**不缺** ✅）

> ## ⚠⚠ 更正：本指南初版这一节是错的
>
> **初版写的是"`io_xclk` 没有任何引脚约束，线是悬空的"—— 那是错的。**
>
> 实际约束在 `video_io.xdc:183`：
> ```tcl
> set_property -dict { PACKAGE_PIN Y18 IOSTANDARD LVCMOS33 } [get_ports io_xclk]
> ```
> **核实方式**（从实现产物回读，不是看文档）：
> ```
> $ grep io_xclk bd_video_wrapper_io_placed.rpt
> | Y18 | io_xclk | High Range | IO_L17P_T2_34 | OUTPUT | LVCMOS33 | ... | FIXED | ...
> ```
> `Constraint = FIXED` —— 约束确实生效了。
>
> **初版为什么错**：我把"某份规格表里没列 io_xclk"当成了"约束文件里没有它"，
> **没有做任何验证就写进了文档，还把它列成阻塞项**。
> 这正是本指南开头警告的那类错误 —— 只不过这次犯的是我自己。
>
> **教训**：判断"某个约束在不在"，要 `grep` 约束文件或回读实现报告，
> **不能凭印象或凭别的文档**。

既然用 PMOD-CAMERA（无晶振），`io_xclk` **必须保留**：

| 项 | 结论 |
|---|---|
| `io_xclk` 约束 | ✅ **保留**（`Y18`） |
| BD 里的 `clk_wiz_xclk` | ✅ **保留**（100 MHz → 24 MHz，M=12/D=1/O=50，VCO=1200 MHz） |
| `Pmod A` 上 `U18`/`W18` 等 | ✅ 正常排布，**没有"白占 IO"的问题** |

> **初版还说 `io_d` 约束了 10 个引脚"越界占了 Pmod A"—— 那也是错的。**
> 实际 `io_d[7:0]` 用的 8 个引脚（W14 Y14 T11 T10 V16 W16 V12 W13）
> **正好是 Pmod B 的全部 8 根**，没有越界。当时是被 XDC 里的注释文字
> （"8bit 数据在 Pmod A"）误导，而代码本身是对的。

**唯一真正待确认的是 `Y17` / `W19`**（Pmod A 上的 `ja[3]` / `ja[7]`）：
它们是 PMOD-CAMERA 的 **NC 脚**，因此**没约束是对的**，不用补。

---


### 1.3 B3 — 寄存器表必须换成真的

> ✅ **2026-09-17 更新**：`rtl/ov5640_regs.v` **已由占位表换为真表**
> （250 条，来源正点原子 `i2c_ov5640_rgb565_cfg.v`，固化为 640×480 RGB565）。
> **本节由此不再是硬阻塞**。
>
> ✅ **2026-09-21 上板进一步排除"表没发出去"**：ILA 抓到
> `sccb_0/cfg_error = 1` —— 配置事务**发出去了**，只是**没收到 OV5640 的 ACK**。
> 所以**问题不在表内容**，而在 **XCLK 是否有时钟 / SDA·SCL 接线是否正确**。
> 判据与下一步见 [`board-test-log-2026-09-21.md`](board-test-log-2026-09-21.md) §5.0。
>
> ---
>
> 以下是**更换前**的记录，保留说明当时为什么要换这张表：

`rtl/ov5640_regs.v` 曾经是**占位表**（`rtl/README.md` 当时写的：
"表里的 8 条寄存器值是猜测，**不足以让 OV5640 出图**"）。

TB 验的是**接口行为**（拼接顺序、位宽、边界），**不验内容** —— 所以 11/11 PASSED 不代表能用。

**症状**：万用表/示波器测得 `io_xclk` 有 24 MHz，但 **`io_pclk` 一直没波形**。
看到这个现象先查寄存器表，别去查 RTL。

**来源**：正点原子例程里的 `ov5640cfg.h`（或其 Linux 驱动 `ov5640.c`）。
需要的是「RGB565 / 640×480 / 30fps」那套配置。

> ⚠ **别只抄前几条**。OV5640 的配置是**有顺序依赖**的，
> 常见的是 `0x3103` 软复位 → `0x3008` 软复位 → 系统时钟 → 时序 →
> 输出格式 → 分辨率 → 才到具体画质寄存器。抄漏顺序会静默不出图。

---

### 1.4 ~~B4~~ — DDR 参数（**已修复并验证** ✅）

**【已核实】** 原 `bd_video.tcl` 里**一条 DDR 参数都没有**，
Vivado 套用了 PS7 出厂默认 `MT41J128M8 JP-125`；
而 PYNQ-Z2 板载是 **`MT41K256M16RE-125`**，对不上。

2026-09-17 已在 `bd_video.tcl` 补上三条并加了回读断言：

```tcl
CONFIG.PCW_UIPARAM_DDR_PARTNO    {MT41K256M16 RE-125}
CONFIG.PCW_UIPARAM_DDR_BUS_WIDTH {32 Bit}
CONFIG.PCW_UIPARAM_DDR_FREQ_MHZ  {533.333}
```

**2026-09-17 21:53 已重跑验证** ✅ —— 脚本输出：

```
>>> DDR 断言通过 (MT41K256M16 RE-125)
```

产物已更新（比特流 + XSA 里都是正确配置）。**这一条不再是阻塞项。**

> 将来若重建工程，判定标准仍然是日志里出现 `>>> DDR 断言通过 (MT41K256M16 RE-125)`。
> **没有这一行就是没生效**，别继续。

---

## 2. 不接电的安全检查

### 2.1 短路检查

万用表**电阻档（或蜂鸣档）**，测 **3V3 对 GND**：

| 读数 | 含义 |
|---|---|
| 呜一声 / < 10 Ω | **短路，绝对不要上电** |
| 几百 Ω 以上 / 逐渐上升 | 正常（电容充电） |

对**摄像头模块**单独测一次，再对**插上之后的板子**测一次。

### 2.2 引脚万用表复核（**烧板风险，不能跳**）

**【未验证】** XDC 的引脚映射是推理的，没实测过。

**只用量电平和通断，不用示波器，也不需要上电**：
【终端：仪器 · 万用表（通断档）】


```
① 万用表 → 通断档
② 表笔黑端接 PYNQ-Z2 的 GND（挑一个板上标了 GND 的排针）
③ 表笔红端逐个点 Pmod A 的 8 个物理针
④ 通的那个就是 GND 列 —— 记下来
⑤ 同样方法找 3V3（接 3.3V 排针）
⑥ 对照 video_io.xdc 末尾的预期表，核对 GND/3V3 是否落在同一列
```

**判定**：如果 GND/3V3 的位置和 `video_io.xdc` 里推的不一致，
**立刻停止，回来改 XDC**，不要"先插上试试"。

> 这一步花 5 分钟，省掉一块板子。

---

## 3. 上电与 PS 侧验证（**先不加载比特流**）

### 3.1 上电顺序
【终端：硬件 · 断电状态下插线，最后才上电】


```
① 插好 SD 卡（PYNQ 镜像，v3.0.1）
② 接串口线（Micro USB，板上标 UART）
③ 打开串口终端（115200 8N1）
④ 最后插 12V 电源
```

**串口终端选哪个** —— 推荐 **MobaXterm（家庭版，免费）**：

| 工具 | 评价 |
|---|---|
| **MobaXterm Home Edition** ✅ **首选** | **串口 + SSH + SFTP 三合一**，而且免费、有免安装便携版。<br>本项目后面要做的三件事它一个顶三个：<br>· **串口**看 PYNQ 启动日志（`Session → Serial`，选 COM 口 + 115200）<br>· **SSH** 登板子跑命令（串口只能看，敲长命令不方便）<br>· **SFTP 拖拽**传 `.bit` / `.hwh`（省掉记 `scp` 命令） |
| PuTTY | 够用且极小，但**只有串口/SSH，没有文件传输** ——<br>传 `.bit` 还得另找 WinSCP。只当串口终端可以 |
| `tio` / `picocom` | Linux/macOS 上的命令行串口工具，简洁可靠。<br>本机是 Windows，用不上 |
| 各类"串口助手"小工具 | ⚠ 只收发热门波特率的能用，但**看不到 Linux 启动全过程的滚屏**，<br>排查"卡在 Loading kernel"时很吃亏 |

> ⚠ **波特率 115200 / 8 数据位 / 无校验 / 1 停止位（8N1）**，流控关掉。
> 这是 PYNQ 镜像的默认值，改了会看到乱码。
>
> ⚠ **没串口就等于没有眼睛** —— PYNQ 起没起来、卡在哪一步，
> 全看串口输出。**先把这个弄通再往下**，不要靠"板子上灯亮了"猜。

### 3.1.1 从启动日志里把 IP 抠出来（`host/pynq_serial.py`）

§3.3 要你「看网口 IP」然后去开 `http://<板子IP>:9090`。麻烦在于
**IP 不一定是你记住的那个**：PYNQ 也可能走 DHCP 分地址，
不以 `192.168.2.99` 为准。

于是每次都要在一屏启动日志里翻 `eth0: <BROADCAST,...>` 那一行，肉眼找四位数字组。
日志滚得快，翻过去就得按 Reset 重来。

`host/pynq_serial.py` 把这件事自动化了 —— 它边收日志边匹配，
**退出时把本次会话出现过的 IP 汇总打印**，并拼好 `http://<ip>:9090`：

【终端：PC · Git Bash】

```bash
python -m pip install --user pyserial   # 一次性
python host/pynq_serial.py              # 先列端口，确认板卡是哪个 COM
python host/pynq_serial.py COM7         # 连（默认 115200-8-N-1）
python host/pynq_serial.py --auto       # 自动挑端口（跳过蓝牙幻影口）
```

> **它不取代 MobaXterm**，也不是完整串口终端：没有滚屏回看、没有文件传输。
> 它只做「日志里找 IP」这一件事。日常用还是 MobaXterm 顺手；
> **只在"IP 又变了、又要重找"的时候用它**。
>
> ### ⚠ 蓝牙串口是幻影口（**这台机器上实际存在**）
>
> Windows 上蓝牙 SPP 会占用 `COM3` / `COM4` 这类**低编号**端口，
> 在端口列表里长得和板卡一模一样。选错了会以为是板卡没反应。
>
> 本机实测：`COM3` / `COM4` **都是蓝牙**，真正的板卡是 FTDI 芯片，
> 描述里应该出现 **`USB Serial Port`**。
> 脚本按 InstanceId 里的 `BTHENUM` 把它们标成 `[蓝牙]`，`--auto` 时跳过。
>
> **看不到 `USB Serial Port` 先查驱动**，不要怀疑板卡。

### 3.2 光一个指示灯就能砍掉一半问题

| 观察 | 含义 |
|---|---|
| 板上 **DONE 灯亮** | 比特流加载成功 |
| **只有 PWR 灯亮** | 卡在 FSBL / 没加载 |
| 串口有 PYNQ 启动日志到 `login:` | Linux 起来了，DDR 正常 |
| 串口卡在 `Loading kernel...` 或乱码 | **多半是 DDR 参数问题** → 回 §1.4 |

> 这一步就能验证 B4。**DDR 错了，Linux 根本起不来。**

### 3.3 进 PYNQ
【终端：串口 · MobaXterm（或 PuTTY）· 115200 8N1】


```bash
# 串口里
login: xilinx
password: xilinx

# 看网口 IP（PC 与板子同网段）
ifconfig
```

**这一步也可以用 §3.1.1 的脚本代替** —— 它会在退出时把日志里的 IP 汇总打印，
省掉肉眼翻屏。手动敲 `ifconfig` 当然也可以。

浏览器开 `http://<板子IP>:9090` → Jupyter。或者直接用串口跑 Python。

### 3.4 在板上跑 Python：**必须 `sudo -E` + 解释器全路径**（2026-09-21 实测）

> ⚠⚠ **这一节是首次上板时前三次失败的全部原因** ——
> 三次报错看起来毫不相干，其实是**同一个根因**：`sudo` 清空了环境。

```bash
# ✅ 唯一正确的调用方式（三要素缺一不可）
sudo -E /usr/local/share/pynq-venv/bin/python3 <脚本> [参数]
```

| 要素 | 为什么缺不得 |
|---|---|
| `sudo` | 加载 overlay / 访问 MMIO 需要 root |
| `-E` | **保留** `XILINX_XRT` 等环境变量 |
| **解释器写全路径** | 绕开 `sudo` 对 PATH 的重置，用 pynq-venv 的 Python |

**三种典型错误写法，报错各不相同但根因同一个**：

| 写法 | 报错 |
|---|---|
| `sudo python3 xxx.py` | `ModuleNotFoundError: No module named 'pydantic'`（PATH 被重置，指向系统 Python） |
| `python3 xxx.py` | `OSError: Root permissions required` |
| `sudo /usr/.../python3 xxx.py`（漏 `-E`） | `RuntimeError: No Devices Found`（`is the XRT environment sourced?`） |

> ⚠ **另有两条相关的坑**：
> - `RuntimeError: Overlay is not downloaded` —— 此版 PYNQ 要求**先加载 overlay**，
>   `allocate()` 才能用
> - **`scp` 要在 PC 上跑**（提示符 `xiaomu@DESKTOP-...`），
>   Python 要在**板子**上跑（提示符 `xilinx@pynq`）。
>   在板子上敲 `scp` 会报源路径找不到。

**同类的环境坑还有**（详见实测记录 §2）：
SD 卡未烧镜像（串口完全无输出）、IP 配错网卡
（笔记本有两块网卡，链路实际走 `以太网 2`（USB 网卡），内置口显示
`Disconnected` **不代表线没插**）。

---

## 4. 加载 overlay

### 4.0 先把两个文件弄出来（**XSA 里就有，不用重新跑**）

PYNQ 要的是 **`.bit` + `.hwh` 两个文件**，它们**都在仓库里的 `gesture_system.xsa` 中** ——
`.xsa` 本质是个 zip，直接解压即可，**不需要重跑综合**：
【终端：PC · Git Bash】


```bash
# 在 PC 上，仓库根目录
cd vivado/gesture_system
unzip -o gesture_system.xsa -d /tmp/xsa_extract

# 看看里面有什么
unzip -l gesture_system.xsa | grep -E "\.bit|\.hwh"
#   bd_video.hwh            744,025 B
#   gesture_system.bit    4,045,692 B     ← 与 build-report 记录的字节数一致，可据此核对
```

> ⚠ **`.hwh` 在 xsa 里叫 `bd_video.hwh`，但必须改名成 `gesture_system.hwh`** ——
> PYNQ 靠**同名**配对找 IP 表：`gesture_system.bit` ⟷ `gesture_system.hwh`。
> 名字不一致时 PYNQ 不会报"找不到 hwh"，而是**只认出 `default` 一个 IP**，
> 然后你在 `ip_dict` 里一个我们的 IP 都看不到 —— 很容易误判成 overlay 有问题。
【终端：PC · Git Bash】


```bash
# 传到板子（改成同名同目录）
scp gesture_system.bit  xilinx@<板子IP>:/home/xilinx/
scp /tmp/xsa_extract/bd_video.hwh xilinx@<板子IP>:/home/xilinx/gesture_system.hwh
```

**核对拿对了没有**：

| 文件 | 大小应为 | 对不上说明 |
|---|---|---|
| `gesture_system.bit` | **4,045,692 字节** | 不是这个数 → xsa 是旧的，重跑 `create_project.tcl` |
| `gesture_system.hwh` | 744,025 字节 | 同上 |

**两个文件必须同名同目录**（PYNQ 靠配对的 `.hwh` 解析 IP 表）。
【终端：PYNQ · Jupyter（浏览器 :9090）或串口】


```python
from pynq import Overlay
ol = Overlay("/home/xilinx/gesture_system.bit")

# ⚠ 先看它到底认出了什么，别猜
print(sorted(ol.ip_dict.keys()))
```

**期望看到**（**【已核实】** 来自 `bd_video.hwh`）：

```
vdma, v_tc, dma_in, dma_out, gesture_preproc_0
```

**立刻记下地址**（后面每一步都要用）：

```python
for n in ["vdma", "v_tc", "dma_in", "dma_out", "gesture_preproc_0"]:
    ip = ol.ip_dict.get(n)
    print(f"{n:20s} {ip}")
```

**【已核实】** `.hwh` 里的地址是这个（**不是** `docs/architecture-contract.md` §2.14 写的 `0x4300_0000`/`0x43C0_0000`，那是原 Sobel 工程的残留）：

| IP | BASE |
|---|---|
| `gesture_preproc_0` | `0x4000_0000` |
| `dma_in` | `0x41E0_0000` |
| `dma_out` | `0x41E1_0000` |
| `v_tc` | `0x44A0_0000` |
| `vdma` | `0x44A1_0000` |

> ⚠ **别把这些地址硬编码进驱动**。项目自己的规则就是
> "地址由 PL 侧驱动分配后写入寄存器告知，不硬编码"（契约 §3.1）。
> 上面这张表只用于**对账**：如果 `ip_dict` 报出来的和这里不一样，说明 overlay 不对。

**版本警告**：PYNQ 镜像与 Vivado 2025.2 的兼容性**【未验证】**。
报版本错时：

```python
ol = Overlay("gesture_system.bit", ignore_version=True)
```

---

## 5. 验证顺序（**这是全文最重要的一节**）

按顺序走。**每一步失败就停下**，不要跳过去试下一步 ——
后面所有现象都会被前面没验证的环节污染。

```
① 加载 overlay，ip_dict 认全           → 本文 §4
② DDR 能分配 buffer，读写一致           → 本文 §5.1      ← 不需要摄像头
③ 只跑预处理链（不碰摄像头）            → 本文 §5.2      ← 不需要摄像头
④~⑧ io_xclk / io_scl / io_pclk / href / vsync / io_d → 本文 §5.3
⑨ VDMA 能抓到帧                        → 本文 §5.4   ← 依赖 ⑥⑦⑧
⑩ 全链路：摄像头 → 96×96               → 本文 §5.5   ← 依赖 ⑨+③
```

| 步骤 | 状态 | 何时做的 / 结果 |
|---|---|---|
| ① overlay | ✅ | 2026-09-21，6 个 IP 全认到，地址与 `.hwh` 逐条吻合 |
| ② DDR 自检 | ✅ | 2026-09-21，**4/4** |
| ③ 预处理链 | ✅ | 2026-09-21，**11/11 全过**，单帧 **≈5 ms** |
| ④~⑧ 摄像头 | ❌ | **未通** —— 见 §5.3 的**更正**：卡在 SCCB，`cfg_error=1` |
| **方案 A（静态图）** | ✅ | **2026-09-23**：板上输出与 golden **逐字节一致**（0/9216，三次实测）。**⑨⑩ 由此绕开** |
| ⑨⑩ | ✅ | **已达成**（走方案 A）：输出 96×96 灰度已实测 + 与 golden 逐位比对通过 |

> ⚠⚠ **③ 的通过不是一次就成的**：第一次上板卡死在 `ap_done` 超时，
> 根因是 **BD 里 AXI DMA 的 `C_SG_LENGTH_WIDTH` 用了默认 14 位**
> （单次上限 16383 B，而帧要传 614400 B）→ 只搬了前 16 KB
> → 下游 IP 等不到剩余输入 → 整条 DATAFLOW 停摆。
> **修复前的比特流（含 `v0.2`/`v0.3` 两个 tag）上板必坏**，
> 必须用 `a4eabe6` 及之后的。
> **完整排查过程见 [`board-test-log-2026-09-21.md`](board-test-log-2026-09-21.md) §三。**

**②③ 不需要摄像头**，所以**摄像头还没到货也能先做掉** —— ✅ **已做掉**。

> ### ✅ ②③ 已脚本化：`host/bringup_check.py` —— 已在真板上跑通
>
> 上面 ②③ 两步的代码**不用手敲**了 —— 已做成可直接跑的脚本：
>
> ```bash
> # ⚠ 三要素缺一不可：sudo + -E + 解释器全路径（见 §3.4）
> sudo -E /usr/local/share/pynq-venv/bin/python3 bringup_check.py \
>      --bit /home/xilinx/gesture_system.bit
> ```
>
> 判定：`*** BRINGUP CHECK PASSED ***`（实测输出：`11 项检查全过`）。
> 它会在每一步给出**针对性的排查提示**（比如输出全黑时列出三个常见原因）。
>
> ⚠ **别用 `sudo python3 bringup_check.py`** —— `sudo` 会重置 PATH 与
> `XILINX_XRT`，报 `No module named 'pydantic'` 或 `No Devices Found`。
> **2026-09-21 前三次失败全是这个**，详见 §3.4。

**注意本文的 § 编号和别的文档会撞车**（比如 `architecture-contract.md` 也有 §5）。
下文凡是引用项目内的其他文档，一律写成 `文件名 §x.y`；
只写 `§x.y` 的都是指**本指南**。引用其他文档的地方是：

| 写在哪 | 指的是 |
|---|---|
| §1.1 表格「出处」列 | `hardware-checklist.md` 第三章、`architecture-contract.md` 第二章 |
| §4 表格上方的注 | `architecture-contract.md` §2.14（那里写的地址**已过时**，勿用） |
| §4 引用契约 | `architecture-contract.md` §3.1 |
| §5.2 启动顺序 | `docs/architecture-contract.md` §4.3 |
| §5.3 上方 | `docs/hardware-checklist.md` 第二章（工具采购建议） |
| §6.1 表格「出处」列 | skill `09-pitfalls.md` 的 C1/C3/E1、`gui-reproduction-guide.md` §2.9 |

---

### 5.1 DDR 与 buffer 基本自检
【终端：PYNQ · Jupyter 或串口】


```python
from pynq import allocate
import numpy as np

buf = allocate(shape=(9216,), dtype=np.uint8)
buf[:] = np.arange(9216, dtype=np.uint8)
print("读回一致:", np.array_equal(buf, np.arange(9216, dtype=np.uint8)))
print("物理地址:", hex(buf.physical_address))
```

**判定**：打印 `True` 且有非 0 物理地址。
**失败** → 回 §1.4，DDR 参数不对。

---

### 5.2 第一步真正的功能验证：**只跑预处理链**（不需要摄像头）

这是**当前最应该先做的事** —— 它把摄像头这个最大的不确定性排除在外。

数据流：

```
DDR(输入 640×480 RGB565) ─► dma_in(MM2S) ─► gesture_preproc ─► dma_out(S2MM) ─► DDR(96×96)
```

**⚠ 启动顺序不能反**（`docs/architecture-contract.md` §4.3）：

```
① 填输入数据 + flush cache
② 先武装 dma_out（S2MM）   ← 必须先，否则预处理输出的第一拍没有接收方
③ 再启动 dma_in（MM2S）
④ 最后 ap_start
⑤ 轮询 ap_done
⑥ invalidate cache，读结果
```
【终端：PYNQ · Jupyter 或串口】


```python
inbuf  = allocate(shape=(640*480,), dtype=np.uint16)   # RGB565
outbuf = allocate(shape=(9216,), dtype=np.uint8)       # 96×96

# 用 host/dump_frame.py 造的测试图当输入，或先填个已知图案
# ... 填 inbuf ...

inbuf.flush()
outbuf.flush()

# ⚠ 顺序：先 S2MM，再 MM2S
dma_out.recvchannel.transfer(outbuf)
dma_in.sendchannel.transfer(inbuf)

gp = ol.gesture_preproc_0
gp.write(0x00, 0x01)          # ⚠ AP_START 在 0x00，不是 0x04

while not (gp.read(0x00) >> 1) & 1:   # ⚠ bit1 = AP_DONE，也在 0x00
    pass

outbuf.invalidate()
```

> ### ⚠⚠ 状态位在 `0x00`，不在 `0x04`
>
> 这是本项目**踩过的大坑**（skill `09-pitfalls.md` D2）：
> HLS 生成的寄存器里**根本没有名为 STATUS 的寄存器**。
>
> | 偏移 | 名称 | 作用 |
> |---|---|---|
> | `0x00` | **CTRL** | 控制 + **状态位就在这里** |
> | `0x04` | GIER | 全局中断使能 —— **不是状态！** |
> | `0x08` | IP_IER | 通道中断使能 |
> | `0x0C` | IP_ISR | 通道中断状态 |
>
> `CTRL` 位域：`bit0=AP_START`(RW) · `bit1=AP_DONE`(RO) · `bit2=AP_IDLE`(RO) · `bit3=AP_READY`(RO)
>
> **别自己给寄存器起名。** 从 `.hwh` 提取真实偏移：
> ```bash
> python <skill>/pynq/ip_contract.py gesture_system.hwh --ip gesture_preproc_0
> ```

**判定**：`outbuf` 里的值和 PC 上 golden 对得上。
【终端：PC · Git Bash】


```bash
# PC 侧
python host/dump_frame.py stats board.bin
python host/dump_frame.py compare board.bin golden.bin
python host/dump_frame.py show board.bin --png out.png
```

**这一步跑通，说明 PL 的预处理链 + 两次 DMA + DDR 通路全部正常。**
后面摄像头出问题就一定能定位在采集侧。

---

### 5.3 摄像头信号逐级排查

> ⚠⚠ **更正（2026-09-21）：本节原写"没有示波器/逻辑分析仪就做不了"。**
> **那是错的 —— 本机两样都没有，照样定位到了 SCCB。**
> 突破口是 **ILA**：它免费、走 JTAG（PYNQ-Z2 那根 Micro-USB 兼作 JTAG）、
> **不需要接线**，而且能看到**外部仪器看不到的内部信号**
> （`sccb_0` / `clk_wiz` 的内部节点都能探）。
>
| | 外部仪器 | **ILA** |
|---|---|---|
| 花钱 | ¥40–80 | **0** |
| 接线 | 要飞线 | **不用** |
| 看内部信号 | ❌ 只能看引脚 | ✅ **内部也能看** |
>
> 所以**先看 §5.3.1**，再回来考虑买不买那 ¥40 的表。

**两种查法，按你手上有什么选一种：**

**A. 有示波器/逻辑分析仪** —— 按下面的表量引脚。

按顺序量，**每一级失败都给出确定的结论**：

| 步骤 | 测哪里 | 期望 | 没有的话说明 |
|---|---|---|---|
| ① | `io_xclk` | 24 MHz 方波 | **PL 侧问题**，与摄像头无关。查 Clocking Wizard（约束在 §1.2 已确认存在） |
| ② | `io_scl` | 有脉冲（配置期间） | `sccb_master` 没在跑。查 `rst_n`、时钟 |
| ③ | `io_pclk` | 有波形 | **摄像头没配上** → 查 `ov5640_regs.v` 表内容（§1.3）和 `sccb_master.cfg_error` |
| ④ | `io_href` / `io_vsync` | 有脉冲 | 配上但没出图 → 查寄存器表的输出格式/分辨率 |
| ⑤ | `io_d[7:0]` | 不是恒 0/恒 1 | 数据线接错或采集时序问题 |

**①②③ 的划分是本节的价值所在**：
- ① 没有 → **PL 的问题**，摄像头完全是无辜的
- ① 有、② 有、③ 没有 → **SCCB 配置失败**，是寄存器表或 SCCB 时序
- ③ 有、④⑤ 没有 → 摄像头在工作，**问题在时序或数据线**

**B. 没有仪器 → 用 ILA（本项目实际走的路，见 §5.3.1）**

---

### 5.3.1 ⭐ 用 ILA 查摄像头（**没有仪器时的唯一出路**，2026-09-21 实操）

**为什么非它不可 —— 软件侧四个观测点全部没接出来：**

| 想看什么 | 在哪 | 软件能读吗 |
|---|---|---|
| `sccb_0` 的 `cfg_done`/`cfg_error` | RTL 输出 | ❌ **无 AXI 接口**（纯 module_ref） |
| `clk_wiz_xclk` 的 `locked` | MMCM 输出 | ❌ **没引出**（`clk_out1` 直连 `io_xclk`） |
| `dvp_capture` 的 `frame_cnt`/`line_cnt` | RTL 输出 | ❌ **BD 里悬空**（作者留了观测点，集成时没接） |
| VDMA 帧计数 | AXI-Lite | ✅ 能读 —— **读了，恒 0** |

**怎么开**：`vivado/bd_video.tcl` 里的 `set use_ila 0` 改成 `1`，重跑
`create_project.tcl`。⚠ **调试完记得改回 0 再重新构建** ——
开着 ILA 会让 BRAM 从 18% 涨到 **52.5%**，报告里的资源数不再代表真实设计。

**⚠ 三个必需的取舍（都是踩出来的）：**

1. **采样时钟用 `FCLK_CLK0`，绝不能用 `io_pclk`**
   —— `io_pclk` 是**摄像头产生的**，摄像头不出图时这个时钟根本不存在
   → ILA 自己也停摆 → 什么波形都看不到。`FCLK_CLK0` 来自 PS，**永远在**。
2. **探针只能接输入方向的信号**
   —— `io_sda` 是双向、`io_xclk`/`io_scl` 是输出，接上去报
   `[BD 41-701] connect_bd_net requires at least two pins`，
   **而且不告诉你是哪个引脚**。
3. **探针要直插嫌疑模块的内部**
   —— 第一版探针全挑"外面看得见的"信号，结果**全静止时无法区分**
   「XCLK 没出」和「SCCB 没配上」（两者在那些探针上长得一模一样）。
   改成直看内部节点（如 `sccb_cfg_error`、`xclk_out`）才有分辨力。

**本项目实测结论**（真表已排除，问题在 XCLK 或接线）：

```
ILA 抓到：sccb_0/cfg_error = 1
  → SCCB 配置事务发得出去，但收不到 OV5640 的 ACK
  → 摄像头从未被初始化 → 不出图 → 无 PCLK → VDMA 帧计数恒 0
```

**尚未区分**（下一步要定的）：

| 可能 | 说明 |
|---|---|
| **A. XCLK 没出** | 摄像头无主时钟 → 不响应 SCCB。项目注释担心过：MMCM 的 VCO=1200 MHz 取到 **-1 速度等级上限**，备选参数 `M=6/D=1/VCO=600/O=25` |
| **B. XCLK 正常，SDA/SCL 接线错** | 引脚映射是**"镜像"推理的，从没实测过**（§2.2 的万用表复核一直没做） |

**下一步**：加两个不受欠采样影响的探针 ——
`clk_wiz_xclk/locked`（静态信号，锁定=1）+ `sccb_0/sda_i`（从机应答）。
`locked` 能一刀切开 A 和 B。

> ⚠ **`iobuf_wrap.v` 是这条链上的已知验证盲区** —— 它例化 Xilinx 原语
> `IOBUF`，iverilog 不认，**功能从没被仿真验证过**，而它正是 SDA 双向那条路的实现。

---

### 5.4 VDMA 抓帧

**前提**：§5.3 的 ⑥⑦⑧ 都通过（有 PCLK/HREF/VSYNC 和有效数据）。

VDMA 是 **3 帧缓存**（`c_num_fstores=3`），地址不硬编码：
【终端：PYNQ · Jupyter 或串口】

> ⚠⚠ **`ol.vdma` 在本版 PYNQ 上直接用不了**（2026-09-21 实测）：
> ```python
> ol.vdma   # AttributeError: 'AxiVDMA' object has no attribute 's2mm_introut'
> ```
> **根因**：PYNQ 的 `AxiVDMA` 专用驱动**构造时硬要求中断**，
> 而**本 BD 里所有中断都悬空**（没接 PS 的 `IRQ_F2P` —— 项目全程轮询，
> 那是**有意设计，不是 bug**）。
>
> **两种可用的替代写法**（都实测过）：

```python
# 写法 A（推荐）：清理 ip_dict 后构造 DefaultIP
#   pop 掉 interrupts / driver —— 它们是 PYNQ 选专用驱动的开关
from pynq import DefaultIP
info = dict(ol.ip_dict['vdma'])
info.pop('interrupts', None)
info.pop('driver', None)
vdma = DefaultIP(info['phys_addr'], info['addr_range'])
# 现成实现见 host/vdma_bypass_test.py / host/camera_probe.py

# 写法 B：裸 MMIO
from pynq import MMIO
vdma = MMIO(info['phys_addr'], info['addr_range'])
```

> ⚠ **VDMA 寄存器偏移要从 `.hwh` 提取，不要凭记忆** ——
> `S2MM_VSIZE` 在 **`0xA0`** 而不是 `0x50`（`0x50` 是 MM2S 的同名寄存器）。

> ⚠ **判据不能是"帧计数非零"** —— 那个值可能是**复位前的陈旧值**。
> 必须是 **"本次运行中是否增长"**（`camera_probe.py` 第二版已改正；
> 第一版有假阳性。实测：基线 1 → 结束 1，变化 **0** 次，缓冲区全零）。

**判定**：把 DDR 里 VDMA 写的帧 dump 出来，用 `dump_frame.py show` 看到**摄像头画面**
（不是全黑、不是噪声）。

---

### 5.5 全链路

§5.4 通过 + §5.2 通过 → 直接跑 `host/gesture_overlay.py`：
【终端：PYNQ · Jupyter 或串口】


```python
from gesture_overlay import GesturePipeline
g = GesturePipeline()
g.print_info()          # 先看 IP 认出来没有
g.setup_dma()
g.config()
g.fill_test_pattern()   # 无摄像头时验数据通路
g.run_once()
g.show()                # Jupyter 里出图
```

结果与 PC golden 对拍：
【终端：PC · Git Bash】


```bash
python host/dump_frame.py stats board.bin
python host/dump_frame.py compare board.bin golden.bin
```

**数据格式**：裸 `.bin` 无头，`uint8`，行优先，96×96 = **9216 字节**。

> `stats` 子命令会打 **4×4 分块均值** —— 这一项能区分
> "整体偏移"和"只有一块有内容（**ROI 位置问题**）"。

---

## 6. 已知会误导你的现象（**读这一节省几小时**）

### 6.1 工具流程全过，不代表能用

本项目的记录里，**至少三类问题在综合/实现/比特流阶段全都静默通过**：

| 现象 | 真实原因 | 出处 |
|---|---|---|
| 比特流正常生成 | DMA 的 `M_AXI_S2MM` 根本没连到 PS 的 HP 口 | skill `09-pitfalls.md` C3 |
| 比特流正常生成 | AXI 互连的 `ARESETN` 悬空 → 被 tie-off 到 0 → 事务一条都过不去 | `GUI复现指南` §2.9 |
| 时序 WNS 是正的 | `apply_board_preset` 把 PS7 配置覆盖了 | skill `09-pitfalls.md` C1 |

**所以**：不要用"工具跑通了"当作通过标准。用 §5 的分级验证。

### 6.2 两条容易误判的报错

| 报错 | 判定 |
|---|---|
| `[Common 17-1257] Failed to create directory 'C'` / `[Common 17-354] Could not open 'C' for writing` | ⚠ **对产物无害，但对流程致命** —— 它会让 `wait_on_run` 抛错、脚本中断。**不要当成纯噪声**。已在 `create_project.tcl` 里加了带 `reset_run` 的重试（详见 §7.1） |
| `[Netlist 29-160] Cannot set property 'iostandard' ... for objects of type 'pin'` ×100 | **忽略**。PS7 IP 自生成 XDC 的作用域约束在顶层无处落地。DDR 约束同样内建在 `.dcp` 里，仍然生效。已在 `create_project.tcl` 里降为 INFO |

**判断一条报错要不要管的通用方法**：
只看 `ERROR` 和 `CRITICAL WARNING` 两种级别，
再用 **`grep -c "^ERROR" impl_1/runme.log`** 数一下。
一个 33KB 的日志里有 111 条 WARNING 但 0 条 ERROR 是**正常**的（skill `09-pitfalls.md` E1）。

### 6.3 `runme.bat` / `runme.sh` 改了没用

它们是 Vivado 每个 run **每次重新生成**的。想改 run 行为要用
`config_*` 参数或 pre-hook，改这两个文件下次就被覆盖。

### 6.4 想复用旧工程目录时

`create_project.tcl` 默认**删掉整个工程目录重建**。
如果 GUI 里改过东西、不想被抹掉，加 `--keep`：
【终端：PC · Git Bash】


```bash
vivado -mode batch -source create_project.tcl -tclargs --keep
```

但注意：**`--keep` 不会让 BD 重新生成**，所以 §1.4 的 DDR 修改不会生效。
改过 `bd_video.tcl` 就必须**不带 `--keep`** 重跑。

---

### 6.5 现象 → 先查哪（**上板时最常翻的一节**）

§5 的每一步都写了「**应该看到什么**」。这一节反过来写
「**实际看到什么 → 先查哪**」—— 因为上板时你手里只有现象。

> 这些不是凭空编的：每一条要么是**本项目真踩过的**，
> 要么已经写进了 `host/bringup_check.py` 的失败分支
> —— 脚本跑到那一步会直接把下面这几行打给你。

#### A. 输出是**全黑**（最常见）

| 先查 | 怎么判断 | 为什么是这个 |
|---|---|---|
| **① `thresh_offset` 的位宽** | `[3c]` 的回读是 `-8` 还是 `248`？ | ⚠ **本项目真踩过**：写成 8 位补码时 IP 读回来是 `+248`，阈值被抬到 255 → 全黑。**且 `ap_done` 照常置位、不报错** |
| **② 输入 buffer 有没有 `flush()`** | 在填完数据之后、`run_once()` 之前有没有刷 | 漏刷时 DMA 搬的是**旧数据**。同样**不报错** |
| **③ 测试图 ROI 区域是不是纯色** | `fill_test_pattern()` 填的是渐变，若你自己换了图 | 纯色 → 阈值后无边缘 → 全黑 |

> 三者的共同点：**都不报错**。所以别问"哪一步炸了"，要按上面顺序**逐个排除**。

#### B. 输出是**全白**

| 先查 | 说明 |
|---|---|
| `thresh_offset` 是很大的负数 | 阈值被压到 0 → 全白 |
| 输入本身就全亮 | 均值阈值失效（均值≈255，任何偏置都过不了） |

#### C. 卡在等 `ap_done` 超时

| 先查 | 说明 |
|---|---|
| **① overlay 版本对不对** | `.hwh` 与 `.bit` **必须配套**（同一次综合产出）。配错时 PYNQ 不报"版本不符"，而是**行为诡异** |
| **② `ip_dict` 里的地址与 `.hwh` 一致吗** | `bringup_check.py` 的 `print_info()` 已经打出来了，对照 §4 那张表 |
| **③ `dma_in` / `dma_out` 有没有认反** | ⚠ 认反了**直接死锁** —— 而且看起来像"IP 没反应" |
| **④ DMA 的 `LENGTH` 写进去没有？** | ⭐ **2026-09-21 实测的真根因** —— 写 `614400` 读回 **8192**（= `写入 mod 16384`）说明 **BD 里 AXI DMA 的 `C_SG_LENGTH_WIDTH` 是默认 14 位**，单次只能传 16383 B。详见下框 |

> ### ⭐ 卡在 `ap_done` 时**第一件要做的事**：读回 DMA 的 LENGTH
>
> **别先怀疑硬件、别先碰摄像头** —— 读一次寄存器就能排除一大类问题：
>
> ```
> 写 LENGTH = 614400  →  读回 8192   ← 写入 mod 16384，中招了
> 写 LENGTH = 123456  →  读回 8768
> 写 LENGTH = 65536   →  读回 0
> ```
>
> **判据**：读回值 = 写入值 **mod 16384** → `C_SG_LENGTH_WIDTH` 是默认 14 位。
> 修复：`bd_video.tcl` 里两个 DMA 都加 `CONFIG.c_sg_length_width {24}`，重出比特流。
>
> ⚠ **这个 bug 的症状极具误导性**：`dma_in` 的 `IOC_Irq` **照常置位**
> （"传输完成"看起来完全正常）、`CTRL=0x01` 显示 IP 确实在跑、
> **一条报错都没有**，只有 `ap_done` 一直不来。
> **修复前的比特流（含 `v0.2`/`v0.3` 两个 tag）上板必坏。**
> 完整排查见 [`board-test-log-2026-09-21.md`](board-test-log-2026-09-21.md) §三。

> ⚠ **这一步不需要摄像头**。若在这里卡住，**不要**去碰摄像头，
> 那只会多引入一个变量。

#### D. 通用判据：怎么看一条报错要不要管

只看 `ERROR` 和 `CRITICAL WARNING` 两种级别：

```bash
grep -c "^ERROR" <run>/runme.log          # 期望 0
grep -E "ERROR|CRITICAL WARNING" <run>/runme.log
```

> 一个 33 KB 的 `runme.log` 里有 **111 条 WARNING 但 0 条 ERROR** 是**正常**的。
> ⚠ 但**别反过来把 ERROR 当噪声** —— §7.1 那个
> `Common 17-1257` 就是"对产物无害、对流程致命"的反例。

---

## 7. 附：本次排查留下的技术结论

### 7.1 `Common 17-1257 / 17-354` —— 对产物无害，**对流程致命**

**【已核实】** 只出现在两个 IP 的 OOC 综合日志，且都在**同一次启动的同一时刻**：

```
runme.log:15  ERROR: [Common 17-1257] Failed to create directory 'C'.
runme.log:16  source bd_video_dma_in_0.tcl -notrace        ← 它跑到这，没退出
runme.log:17  create_project: Time (s): cpu = 00:00:11     ← 正常返回
```

证据链（**产物层面确实无害**）：
1. `source` 在**第 3 行**就执行了，报错在第 15 行 —— 说明它发生在 run 脚本**之外**
2. 返回码 0，脚本继续跑完，末尾 `synth_design completed successfully`
3. 产物齐全（`.dcp` / `_sim_netlist.v` / `_stub.v` / `__synthesis_is_complete__`）
4. 每次运行只有 2–3 个 OOC run 中招，**中招的也照样完成了**

**原因**：一次性启动 16–22 个 OOC 综合，每个 Vivado 实例都要建一批临时目录——
**并发建目录竞争 / 杀软-文件索引拦截**的瞬时失败。重跑即成功。

**已排除**：`TEMP`/`TMP` 正常（`(系统临时目录)`）；
没有空环境变量会被展开成裸盘符；工程脚本里没有任何地方传过 `"C"`。

#### ⚠⚠ 更正（2026-09-17，实测推翻初版结论）

**初版本指南写的是"100% 无害，直接忽略"—— 那是错的。**

它对**产物**无害，但对**流程致命**：

```
ERROR: [Vivado 12-13638] Failed runs(s) : 'bd_video_ic_ctrl_imp_xbar_0_synth_1'
ERROR: [Common 17-39] 'wait_on_runs' failed due to earlier errors.   ← 脚本在此中断
```

Vivado 会把这个瞬时失败**判定成 run 失败**，于是 `wait_on_run` **直接抛错返回**，
脚本中断 —— **XSA 导出和资源报告根本没执行到**。
2026-09-17 实际发生过：比特流成功生成了，但没有 `.xsa`。

**初版为什么误判**：当时的依据是"产物齐全 + 返回码 0"，
**没有考虑它对上层批处理流程的影响**。
教训：一个错误"是不是无害"，要看**谁在消费它的后果**，不能只看直接产物。

**现在的处理**（`create_project.tcl` 里）：

```tcl
proc run_with_retry {run_name launch_args {max_attempts 3}} {
    for {set attempt 1} {$attempt <= $max_attempts} {incr attempt} {
        if {$attempt > 1} { catch {reset_run -quiet $run_name} }   ;# ← 关键
        catch {launch_runs $run_name -jobs 8 {*}$launch_args}
        catch {wait_on_run $run_name}
        ...
    }
}
```

⚠ **`reset_run` 是必需的，漏了就变成"假重试"**：
`launch_runs` 对**已经失败的 run 不做任何事**（它认为"跑过了"）。
实测日志：

```
>>> synth_1 进度 0% —— 第 1 次未完成
>>> 重试...
[21:38:34] Waiting for synth_1 to finish...
[21:38:34] synth_1 finished        ← 同一秒返回，根本没跑
>>> synth_1 进度 0% —— 第 2 次未完成
```

先 `reset_run` 清掉失败状态，`launch_runs` 才会真正执行。
（已完成的子 run 不受影响，只有失败的需要重跑。）

---

### 7.2 `Netlist 29-160` —— 无害，但掩盖了一个真问题

**【已核实】**

- 报错行 `bd_video_ps7_0.xdc:29` 是 `set_property iostandard "SSTL15_T_DCI" [get_ports "DDR_VRP"]`
- 这份 XDC 是 **PS7 IP 自动生成**的（`set_property PIO_DIRECTION` 是 PS7 专有属性）
- OOC 综合阶段 **0 报错**（`bd_video_ps7_0_synth_1` / `dma_in` / `dma_out` 都查过）
- 只在顶层 `link_design` 读 `.dcp` 时炸 —— 顶层 36 个 user IO 里 **没有一个是 `DDR_*`**，约束无处落地
- 顶层 35 个 logical port 中 22 个是 `hdmi_vid_out_*`（本来就悬空、走 DRC 豁免）
- DDR 引脚/电平约束**同时内建在 `.dcp` 里**，仍然生效
- 那次实现 **ERROR 0 条**，比特流正常生成

**但真正的问题是**：这 100 条告警刷屏，把 §1.4 那个 DDR 参数错误**盖住了**。
`ps7_parameters.xml` 里写着 `MT41J128M8 JP-125`（默认值），
而 `bd_video.tcl` 里根本没有 DDR 配置 —— 一条告警都没提这件事。

### 7.3 已完成的修改（2026-09-17）

| 文件 | 改动 |
|---|---|
| `vivado/bd_video.tcl` | 补 PS7 DDR 三条参数 + **回读断言**（写错就早失败，不静默走默认值） |
| `vivado/create_project.tcl` | 加 `set_msg_config -id {Netlist 29-160} -new_severity INFO` |

> ⚠ 两处修改都**只改了脚本**。**现有比特流（2026-09-16 21:25）里是旧配置**，
> 必须按 §1.4 重跑才能生效。

---

## 8. 待验证清单（按"说清楚什么没验证"的原则）

| 项 | 状态 |
|---|---|
| 板上时序余量 | ⚠ **RTL 级** WNS = **+0.265 ns**、WHS = +0.051 ns（2026-09-17 最后一次实现，`All user specified timing constraints are met`）。⚠ **逐次波动大**（+0.873 / +1.177 / +0.265，布线是随机的）。⚠⚠ **且该 WNS 属于 AMD `v_tc` IP 内部，不是本设计的余量**（见 `report/design.md` §4.5）；本项目 HLS 流水线余量 +43%。**板级功耗/温度实测仍未做** |
| PS7 DDR 参数 | ✅ **已修复并重跑验证**（§1.4），XSA 里是 `MT41K256M16 RE-125` |
| 摄像头模块选型 | ✅ **已定：PMOD-CAMERA v1.0，直插 Pmod A+B**（2026-09-17，§1.1） |
| `io_xclk` 引脚约束 | ✅ **存在**（`video_io.xdc:183` = `Y18`，实现报告 `Constraint=FIXED`）。初版误判为"缺失"已更正（§1.2） |
| ~~`ov5640_regs.v` 寄存器表~~ | ✅ **2026-09-17 已换为真表**（250 条，固化 640×480）。✅ **2026-09-21 上板确认事务发出、但无 ACK**（`cfg_error=1`）→ 问题在 XCLK/接线 |
| 引脚映射（万用表复核） | ❌ **未做**（§2.2）—— ⚠ **当前故障的候选原因之一** |
| PYNQ 镜像与 2025.2 兼容性 | ⚠ **部分**：overlay 能加载并认全 6 个 IP；**未做版本专项验证** |
| HDMI 输出 | ❌ 未实现（BD 里没有 TMDS 编码器；22 个端口在比特流里悬空，**上板不要接 HDMI 线**） |
| 板上实测 ②③ | ✅ **2026-09-21 通过**：DDR 自检 4/4 + 预处理链 **11/11**，单帧 ≈5 ms（**不需要摄像头**） |
| 板上实测 ④~⑧ | ❌ **未通**：摄像头不出图，ILA 定位到 `sccb_0/cfg_error=1`（见实测记录 §5） |
| 建工程→综合→实现→比特流→XSA 全流程 | ✅ **2026-09-17 完整跑通**（0 error / 0 critical warning，顶层 `bd_video_wrapper`） |

---

## 9. 一页速查
【终端：PC · Git Bash】


```bash
# ---- 建工程 + 出比特流（改了 bd_video.tcl 之后必须重跑）----
cd <仓库根>/vivado
vivado -mode batch -source create_project.tcl
#   只看 DDR 断言过没过：
#     >>> DDR 断言通过 (MT41K256M16 RE-125)

# ---- 只检查 BD 是否合法（约 1 分钟，不跑综合）----
vivado -mode batch -source create_project.tcl -tclargs --synth 0

# ---- 没板子也能跑的自检 ----
bash rtl/run_iverilog.sh              # 判定：*** TB PASSED ***（finished 不算）
bash sw/build_preproc_sim.sh          # 判定：*** PREPROC DRIVER SIM PASSED ***

# ---- PC 侧数据处理 ----
python host/dump_frame.py stats board.bin
python host/dump_frame.py compare board.bin golden.bin
python host/dump_frame.py show board.bin --side-by-side golden.bin --png cmp.png
```
【终端：PYNQ · Jupyter 或串口】


```python
# ---- 板上 PYNQ ----
from pynq import Overlay, allocate
ol = Overlay("gesture_system.bit", ignore_version=True)
print(sorted(ol.ip_dict.keys()))
# 期望：vdma, v_tc, dma_in, dma_out, gesture_preproc_0
```

**验证顺序一句话**：

```
先跑 §5.2（预处理链，不要摄像头）→ 通了再碰摄像头
摄像头按 io_xclk → io_scl → io_pclk → HREF/VSYNC → 数据 逐级量
```

**三个"看到就停"的信号**（完整对照表见 **§6.5**）：

| 看到 | 说明 |
|---|---|
| 串口卡在 `Loading kernel...` | DDR 参数不对 → §1.4 |
| `io_xclk` 没波形 | PL 问题，**别去动摄像头** |
| `io_pclk` 没波形但 XCLK 有 | 查寄存器表内容 / SCCB 接线 / 时序 → §1.3 |

> ⚠ 这三条是**摄像头相关的**信号。板到当天更可能先遇到的是
> **输出全黑 / 卡在 ap_done** —— 那两类在 **§6.5**。

---

## 10. 打印版（Word 操作手册）

本文还有一份**排版好的 Word 版**，适合打印出来带到工位上：
【终端：PC · Git Bash（或直接看仓库里的文件）】


```
docs/上板测试操作手册.docx
```

**它是从本文自动生成的，不是手工维护的第二份。**
改了本文件之后重跑一次即可：
【终端：PC · Git Bash】


```bash
python scripts/build_manual.py      # 生成 docx
python scripts/test_manual.py       # 校验没有内容在渲染中丢失
```

> ⚠ **不要直接改 Word**。Word 版是渲染产物，
> 手工改的内容下次重新生成就没了，而且两份会开始分叉 ——
> 这个项目已经在"双目录分叉"上吃过一次亏。
> **要改内容就改本文件（`.md`），它是唯一事实来源。**

Word 版针对打印做的处理：

| 项 | 说明 |
|---|---|
| 版面 | A4、页边距 2cm、正文 10.5pt |
| 分页 | **每个二级标题另起一页** —— 手上拿一张做一个阶段 |
| 代码块 | 等宽 + 灰底 + 左边框（终端命令抄错一个字符就白跑一轮） |
| 引用块 | 左边框高亮（本手册的"⚠ 注意"全在引用块里） |
| 页眉页脚 | 文档名 + 第 X 页 / 共 Y 页 |
| **手填记录表** | 万用表读数、示波器频率这类**当场要记的数**，<br>每个相关小节末尾都有一张空白表 |

> ⚠ **不生成目录（TOC）**。python-docx 生成的是"域"，
> Word 打开时要手动按 F9 更新才显示页码 ——
> 打印前忘了更新会印出一页空白。与其埋这个坑，不如自己在 Word 里插。
> 页脚的页码同理，打开后 Ctrl+A → F9 更新一次即可。
