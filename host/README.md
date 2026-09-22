# host 索引

> PC 侧工具。几个文件各管一件事 —— **互相独立，可以单独用**。
>
> ⚠ **`bringup_check.py`、`pynq_serial.py`，以及那批诊断脚本不在
> 下面那个数据流里** —— 它们服务的是**上板当天**（板子起没起来、IP 是多少、
> 摄像头有没有出数），不是算法本身。
> 见 `docs/board-bringup-guide.md` §3.1.1 / §5。

## 算法主线：三个工具的关系

```
   ┌─────────────────────────────────────────────────────┐
   │  gesture_golden.py     Python 参考实现（第三方对拍） │
   │      ↓ 产出 .bin                                     │
   │  dump_frame.py         帧比对/出图（两侧共用）        │
   │      ↑ 读 .bin                                       │
   │  gesture_overlay.py    板上驱动（PYNQ）              │
   └─────────────────────────────────────────────────────┘
```

| 文件 | 在哪跑 | 作用 |
|---|---|---|
| `gesture_golden.py` | **PC** | 预处理链的 Python 参考实现 |
| `dump_frame.py` | **PC + 板** | 帧数据的转储、比对、出图 |
| `gesture_overlay.py` | **板（PYNQ）** | 加载 overlay、驱动整条流水线 |
| `pynq_serial.py` | **PC** | 串口控制台，从启动日志里抠板卡 IP |
| `bringup_check.py` | **板（PYNQ）** | 分步验证（DDR / 预处理链）—— **②③ 的入口** |

**上板诊断脚本**（⚠ 都是 **2026-09-21 首次上板那天为排查故障现写的**，
一次性倾向较重，但每个都解决了一个**当时没有其它手段能问的问题**）：

| 文件 | 作用 | 是否还有用 |
|---|---|---|
| `len_loopback.py` | 只测 `dma_in` 的 LENGTH 能否正确读写 | ⭐ **留着** —— 查出那个 bug 的关键一步 |
| `dma_mmio_check.py` | 确认 `dma_in`/`dma_out` 的 MMIO 到底指向哪块地址（排除地址混叠） | 留档（已排除的假设） |
| `reg_ident.py` | 逐寄存器探针，弄清 DMA 地址映射 | ⚠ **含我自己的误读**，见下 |
| `camera_probe.py` | 摄像头通路：VDMA 帧计数**本次运行中是否增长** | ⭐ **留着** —— 摄像头排查的主工具 |
| `vdma_bypass_test.py` | 找出本版 PYNQ 上访问 vdma 寄存器的可用方式 | ⭐ 留着（绕开中断依赖的方法） |

> ⚠ **还有两个当天用过但没进仓库**：`ap_done_probe.py`、`dma_diag.py`
> —— 它们**只存在于 `docs/board-test-log-2026-09-21.md` 的记载里**
> （前者是定位故障的第一步，后者的结论已被作废）。
> **要复现那一步的话，需要照实测记录 §5.3 的说明重写**，仓库里没有现成的。

> ⚠ **`reg_ident.py` 你自己看的时候要留意**：`ap_done_probe` 曾报
> "MM2S_DMACR 写 `0x1001` 读回 `0x00011003`"，据此怀疑地址映射错了 ——
> **那个异常后来查明是我读错了寄存器**（见实测记录 §8 的订正），
> 不是地址问题。脚本本身没问题，**是它的结论作废了**。

> ⚠ **这批脚本大多没有 UTF-8 兜底守卫**（见文末那节）——
> 在板上（Linux/UTF-8）跑没问题，**在中文 Windows 上跑可能抛 `UnicodeEncodeError`**。

> ### ⚠ 板上跑 Python：三要素缺一不可
>
> ```bash
> sudo -E /usr/local/share/pynq-venv/bin/python3 <脚本> [参数]
> ```
>
> `sudo`（要 root）+ `-E`（保留 `XILINX_XRT`）+ **解释器全路径**
> （绕开 `sudo` 的 PATH 重置）。反例：`sudo python3 xxx.py` 会报
> `No module named 'pydantic'`；少了 `-E` 会报 `No Devices Found`。
> **2026-09-21 实际踩过，前三次失败全是这个。**

---

## PYNQ 的专用驱动会硬要求中断 —— 本项目没接中断

`ol.vdma` 报 `AttributeError: 'AxiVDMA' object has no attribute 's2mm_introut'`，
根因是 **PYNQ 的 `AxiVDMA` 专用驱动硬要求中断**，
而本项目 BD 里**所有中断都悬空**（全程轮询，是**有意设计**，不是 bug）。

**对策**（见 `camera_probe.py`）：**pop 掉 `ip_dict` 里的
`interrupts` / `driver` 两个字段再构造 `DefaultIP`**
—— 它们是 PYNQ 选专用驱动的开关。裸 `MMIO(phys_addr, range)` 同样可用。

> ⚠ **VDMA 的 `S2MM_VSIZE` 在 `0xA0` 不是 `0x50`**（`0x50` 是 MM2S 的同名寄存器）。
> 偏移要从 `.hwh` 提取，**不要凭记忆**。

---

## gesture_golden.py —— 第三方对拍

**为什么需要它**：HLS 的 csim 是"HLS 实现 vs C++ golden"比对。
如果那个 C++ golden 本身理解错了，两边会**一起错**，测试照样通过。

Python 版本是**完全独立的第二实现**，三者互证：

```
        HLS 实现
       ↙        ↘
C++ golden      Python golden
```

```bash
python gesture_golden.py --self-test                       # 自检（无需文件）
python gesture_golden.py --input f.bin --width 640 --height 480 \
                         --out result.bin --png result.png
```

---

## dump_frame.py —— 帧比对工具

**用在两个地方**，这正是它的价值：板上 dump、PC 比对 ——
**同一份数据文件**，能直接回答"是 PL 传错了还是 CNN 读错了"。

```bash
python dump_frame.py make-test --out test.bin        # 造可预测的测试帧
python dump_frame.py stats board.bin                 # 统计 + 4x4 分块均值
python dump_frame.py compare board.bin test.bin      # 逐像素比对
python dump_frame.py show board.bin --png out.png    # 出图（放大 4 倍）
python dump_frame.py show board.bin --side-by-side golden.bin --png cmp.png
```

### 数据格式（与 CNN 侧的契约）

```
裸 .bin，无文件头
uint8，96x96，行优先
共 9216 字节
```

> **不加文件头是故意的** —— 加了就要两侧同步解析逻辑。
> 裸 `.bin` 任何工具（含 HLS 的 C 代码）都能直接 `fread`。

### ⚠ 中文 Windows 的一个坑

`stats` 里的标记用的是 **ASCII `[OK]` / `[!!]`，不是 `✓` / `⚠`**。

原因：中文 Windows 的控制台编码是 **GBK**，打印 `✓`（U+2713）会抛
`UnicodeEncodeError: 'gbk' codec can't encode character`。

写任何面向中文 Windows 的 CLI 工具都要注意这条 ——
**Unicode 符号在 GBK 控制台下会直接崩**。

### `stats` 的 4×4 分块均值有什么用

能快速区分两类完全不同的故障：

| 现象 | 多半是什么 |
|---|---|
| 只有某一块有内容，其余全 0 | **ROI 位置错了**（裁剪窗口没对准） |
| 整体均匀但全黑/全白 | **阈值问题** |
| 分块值呈梯度但有偏移 | **数据流对齐问题** |

---

## gesture_overlay.py —— PYNQ 板上驱动

```python
from gesture_overlay import GesturePipeline
g = GesturePipeline()              # 加载 overlay
g.print_info()                     # 先看 IP 认出来没有
g.setup_dma()                      # 分配 DMA 缓冲
g.config()                         # 配参数
g.fill_test_pattern()              # 无摄像头时填测试图
g.run_once()                       # 跑一帧
g.show()                           # Jupyter 里出图
```

### 三个设计点

**1. 地址不硬编码** —— 全部从 `overlay.ip_dict` 读。
重新综合后地址会变，硬编码必然出错。

**2. 执行顺序不能反**（与裸机 C 驱动同一套）：
```
① 先武装 dma_out(S2MM)  ② 再启 dma_in(MM2S)  ③ 最后 ap_start
```
反了的话预处理输出的第一拍没有接收方，数据会丢。

**3. 用 `pynq.allocate` 而不是 numpy 数组** ——
`allocate` 出来的 buffer 是 **cache 一致的**，不用手动 flush/invalidate。
这是 PYNQ 相对裸机最大的便利（裸机漏了 cache 维护会拿到旧数据，且不报错）。

> ⚠ **`ignore_version=True` 可能需要**：PYNQ 镜像基于某个 Vivado 版本，
> 与本项目的 2025.2 不一致时 `Overlay()` 会报版本错误。先不加，报错再加。

> ⚠ **若 `io_pclk` 没有波形**：先查 `rtl/ov5640_regs.v` 的配置表。
> ✅ 2026-09-17 已由占位表换成真表（250 条，固化 640×480 RGB565）。
> ✅ **2026-09-21 上板已排除"表没发出去"**：ILA 抓到 `sccb_0/cfg_error=1`，
> 说明事务**发了但没收到 ACK** → 问题不在表内容，在 **XCLK 或接线**。
> 用 `fill_test_pattern()` 可在无摄像头时验证数据通路（**已实测通过**）。

---

## pynq_serial.py —— 串口控制台（上板当天用）

```bash
pip install --user pyserial
python pynq_serial.py            # 列端口，确认板卡是哪个 COM
python pynq_serial.py COM7       # 连（默认 115200-8N1）
python pynq_serial.py --auto     # 自动挑端口（跳过蓝牙幻影口）
```

**它只做一件事**：边收启动日志边匹配 IP，**退出时汇总打印** `http://<ip>:9090`。
这解决了 `docs/board-bringup-guide.md` §3.3 的痛点 ——
PYNQ 的 IP 不一定是你记住的 `192.168.2.99`（可能走 DHCP），
而启动日志滚得快，肉眼翻 `eth0:` 那一行翻过去就得按 Reset 重来。

> **不是完整串口终端**：没有滚屏回看、没有文件传输。
> 日常还是用 MobaXterm（§3.1 推荐），**只在"IP 又变了、又要重找"时用它**。

### ⚠ 蓝牙串口是幻影口

Windows 上蓝牙 SPP 会占用 `COM3` / `COM4` 这类**低编号**端口，
长得和板卡一模一样，**选错会以为是板卡没反应**。

真正的板卡是 **FTDI** 芯片，描述里应出现 `USB Serial Port`。
看不到它先查驱动，不要怀疑板卡。脚本按 InstanceId 里的 `BTHENUM` 标记/跳过。

---

## 三个工具的共同约定

参数常量（尺寸、寄存器偏移）都**硬写在各自文件里**，来源是：

| 常量 | 来源 |
|---|---|
| 96×96 / 9216 | `src_hls/gesture_preproc.h` |
| 寄存器偏移 | 官方 `xgesture_preproc_hw.h` |
| 控制位语义 | 官方 `xgesture_preproc.c` |

**改任何一处都要同步改另外两处和相关源码。**
这些值在 `gesture_golden.py` / `gesture_overlay.py` / `dump_frame.py`
里各自定义了一遍 —— 是有意为之：**三个工具要能独立运行**，
不依赖共享模块（那样任一文件缺失就全不能用）。

### ⚠ 控制台 UTF-8 兜底：**只在自己是最外层时才包**

`host/` 与 `scripts/` 下的脚本都在开头包了一层 UTF-8
（中文 Windows 控制台是 GBK，打印 `⚠` `→` 会直接 `UnicodeEncodeError`）：

```python
if getattr(sys.stdout, 'encoding', '').lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
```

**⚠⚠ 那个 `if` 判断不能省。** 省了会踩这个坑：

```
调用方先包一层     →  sys.stdout = W1(裸 buffer)
被 import 的模块再包  →  W2 = W1.buffer 又包一层
```

两层 wrapper 共享同一个底层 buffer，**其中一层被 GC 回收时会把 buffer
一起关掉**，于是调用方最后打印汇总时抛：

```
ValueError: I/O operation on closed file
```

**2026-09-19 实际踩过**：`python host/test_pynq_serial.py`
（不带 `PYTHONIOENCODING=utf-8`）必崩 ——
测试模块先包了一层，它 import 的 `pynq_serial` 又包了一层。
加上 `PYTHONIOENCODING=utf-8` 就正常，因为那种情况下第一层不会执行。

> **这条之所以值得单独写**：当时项目里 **7 个脚本**各自复制了这段代码，
> **5 个没有防护**。而且它只在"被 import"时才发作 ——
> 单独跑那个脚本永远测不出来。
>
> **没有抽成共享模块**，理由与上面一致：`host/` 和 `scripts/` 是两个目录，
> 跨目录 import 要额外塞 `sys.path`，**比重复两行守卫更脆**。
> 真正值钱的是这段解释，不是那两行代码。
