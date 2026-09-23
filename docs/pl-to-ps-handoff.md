# PL → PS 侧交付说明

> **面向**：负责 PS 侧 CNN 的人
> **日期**：2026-09-23
> **本仓**：`gesture_recognition_system`（PL 侧，Apache-2.0）
>
> **一句话**：PL 侧的图像预处理链**已经板上实测验证完毕**，
> PS 侧现在就可以**在 PC 上开始开发 CNN** —— 大部分工作不需要板子。

---

## 一、分工边界

| 责任方 | 范围 |
|---|---|
| **本仓（PL 侧）** | DVP 采集、图像预处理链、VDMA、HDMI、DDR 中继 buffer |
| **PS 侧（你）** | CNN：模型训练、量化、推理、后处理 |

**边界只有一条**：DDR 里一块 **96×96 uint8 灰度 buffer（9216 字节）**。

两侧各自把它当黑盒 —— **这是本项目最重要的设计决策**：
你可以在 PC 上独立开发调试，**不受板子是否到货影响**。

---

## 二、即刻可做（不需要板子）

### 2.1 数据格式

| 项 | 值 |
|---|---|
| 形状 | `96 × 96`，行优先连续存放 |
| 类型 | `uint8` |
| 取值 | `0–255`，**不做归一化**（归一化由你负责） |
| 字节数 | `9216` |
| 对齐 | 4 字节（DMA 要求） |

### 2.2 直接可用的样例

仓库 `samples/` 下：

```bash
samples/frame_640x480_rgb565.bin   # 输入样本，614400 B
samples/golden_96x96_gray.bin      # 期望输出，9216 B
```

```python
import numpy as np
rgb  = np.fromfile('samples/frame_640x480_rgb565.bin', dtype='<u2').reshape(480, 640)
gray = np.fromfile('samples/golden_96x96_gray.bin',     dtype=np.uint8).reshape(96, 96)
```

> ⭐ **这份 golden 是硬件实测对过的，不是推算的**：
> 它与 PYNQ-Z2 板上输出的 **md5 完全相同**（`ca5df4fc5fec792ccd4fcb2090bcae25`）。
> 三方一致链：板上输出 == C++ golden == Python golden。

### 2.3 ⭐ 批量造训练数据（**这一步最重要**）

`host/gesture_golden.py` 是**可 import 的库**，输出与硬件**逐位一致**：

```python
import importlib.util
import numpy as np

spec = importlib.util.spec_from_file_location('G', 'host/gesture_golden.py')
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

def to_feature(rgb565_640x480, roi=(160, 80, 320, 320), thresh_mode=1):
    """任意 640×480 RGB565 图 → 硬件同款 96×96 特征图"""
    return G.gesture_preproc(rgb565_640x480, 640, 480, *roi,
                             thresh_mode=thresh_mode)

f = to_feature(rgb)          # shape (96,96) uint8
```

**为什么可信**：这条 Python 实现与 C++ golden 逐位一致，
而 C++ golden 与板上输出逐字节一致。
**所以你在 PC 上造的数据，分布就是硬件真实产出的分布。**

> **数据增强的一个天然手段**：`roi` 参数可以变。
> 实测同一张图不同 ROI 给出明显不同的特征图
> （`(160,80,320,320)` → 非零 2665；`(96,32,448,448)` → 1992；
> `(0,0,640,480)` → 1695）。
> **但训练时的 ROI 必须与推理时硬件配置的一致**，抖动幅度要小。

---

## 三、⚠️ 动手前必须拍板的一件事：`thresh_mode`

**这决定 CNN 能学到什么，且不是纯软件开关**（是 PL 寄存器 `0x20`）。

| `thresh_mode` | 输出 | 取值种类 | 非零像素 | CNN 学到 |
|---|---|---|---|---|
| **1（当前默认）** | **二值** | **2**（只有 0/255） | 2665 / 9216 | 手形轮廓/拓扑，**对光照天然鲁棒** |
| 0 | 灰度直通 | 99 | 8616 / 9216 | 轮廓 + 边缘强度，依赖 gain/曝光一致 |

### 建议：先按 `thresh_mode=1`（二值）做

理由：

1. **手势分类本质是形状问题**，轮廓信息基本够用
2. **对光照/肤色变化天然免疫** —— 没有受控光照时这是实打实的优势
3. 96×96 的小图，灰度多出的信息量未必换来相应收益

### 但值得花 20 分钟先对比一下

```bash
# 灰度版（--no-thresh 关掉二值化）
python host/gesture_golden.py --input samples/frame_640x480_rgb565.bin \
       --roi 160 80 320 320 --no-thresh --out golden_gray.bin
```

两种模式各生成一批特征图，肉眼比较区分度再定。

> ⚠⚠ **训练集与推理时必须用同一种模式**，否则数据分布不一致，模型白练。
> 这个决定**越早定越好** —— 改它要在 PL 侧重跑一次板上流程。

---

## 四、后续联调（需要板子）

### 4.1 你需要的硬件接口

预处理链是 HLS IP `gesture_preproc`，配两个 AXI DMA：

| IP | 作用 | AXI-Lite 基地址 |
|---|---|---|
| `gesture_preproc` | 预处理链 | `0x40000000` |
| `dma_in` | MM2S：DDR → 预处理输入 | `0x41E00000` |
| `dma_out` | S2MM：预处理输出 → DDR | `0x41E10000` |

> ⚠ **地址别硬编码** —— 重新综合后会变。从 `overlay.ip_dict` 读。

### 4.2 执行顺序（**不能反**）

```
1. 分配 DDR 缓冲：输入 614400 B（640×480 RGB565）、输出 9216 B
2. 写预处理参数（WIDTH / HEIGHT / THRESH / ROI ...）
3. 【先武装 S2MM】dma_out：DSTADDR / DMACR.RS=1 / LENGTH=9216
4. 【再启 MM2S】dma_in ：SRCADDR / DMACR.RS=1 / LENGTH=614400
5. 【最后 ap_start】写 gesture_preproc CTRL(0x00) bit0 = 1
6. 轮询 CTRL(0x00) bit1（ap_done）直到置位
7. 读输出 buffer —— 这就是喂给 CNN 的 96×96 灰度
```

**3→4→5 顺序不能反**：S2MM 没先武装，预处理输出的第一拍没有接收方，会丢数据。

### 4.3 两个已知的坑

**① 状态位在 `CTRL(0x00)`，不在 `0x04`**
`0x04` 是 `GIE`（全局中断使能）。本项目在早期驱动上踩过：
把 `0x04` 当状态读，轮询永远等不到，而仿真自己造假值掩盖了它。

**② cache 一致性**
DMA 绕过 CPU cache。写完输入必须 **flush**，读完输出必须 **invalidate** ——
漏了会拿到旧数据，**且不报任何错**。

> 完整寄存器表（16 个偏移）见 `docs/architecture-contract.md` §3.2。
> 参考实现：`host/gesture_overlay.py`（PYNQ Python）、
> `sw/preproc_driver.c`（裸机 C）—— **两者是同一套顺序，改一处要同步另一处**。

---

## 五、当前 PL 侧状态（如实说明）

| 项 | 状态 |
|---|---|
| HLS 预处理链 | ✅ csim 7 组用例 + csynth 全 II=1 + **cosim 10/10** |
| **板上输出 vs golden** | ✅ **逐字节一致** —— 三次不同输入 / 三种 ROI，各 **0/9216 不一致** |
| 静态图通路（方案 A） | ✅ **充分验证** —— 图片直接写 DDR，**不需要摄像头** |
| 摄像头（DVP） | ❌ **未通**（SCCB 无 ACK），**但不阻塞** —— 赛题未要求必须用摄像头 |
| HDMI 输出（TMDS） | ⚠️ 未做 —— **上板不要接 HDMI 线** |
| 资源 / 时序 | LUT 45.94% / FF 28.85% / BRAM 18.21% / DSP 27.73%；WNS +0.199 / WHS +0.051 |

> ⚠ **仓库里 `vivado/gesture_system/gesture_system.xsa` 是旧版**（不含最近两个修复）。
> 上板要用的正确比特流**另存**在 PL 侧本机（`E:\board_test\verified_v0.4\`，
> md5 `23d25563a4b0362da837544f3a7a80d0`），**我还没准备好正式交付形态** ——
> 联调前跟我说，我处理好再给你。

---

## 六、你这边的建议路线

```
① 定 thresh_mode（1 还是 0）          ← 最优先，影响后续全部
② 用 gesture_golden.py 批量造数据
③ PC 上训练 + 验证（完全不需要板子）
④ 定下模型后：量化 → 评估 96×96 小图上的推理耗时
⑤ 联调：板 → DDR → 你的推理 → 结果
```

> **第 ③ 步之前的所有事，你现在就能做，不用等我们。**

---

## 七、文件索引

| 想看什么 | 去哪 |
|---|---|
| 接口契约（**权威定义**） | `docs/architecture-contract.md` §3 |
| 样例数据说明 | `samples/README.md` |
| 板上实测记录（含两个 bug 的根因） | `docs/board-test-log-2026-09-23.md` |
| 综合与实现报告（赛题要求） | `vivado/build-report.md` |
| 上板操作指南 | `docs/board-bringup-guide.md` |
| 踩坑合集 | 根 `README.md` 的「最值钱的坑」 |

---

## 附：有问题先查这几条

1. **造出的特征图和 samples/ 里的对不上** → 检查 ROI 是否一致（生成输入用的图 / 你的 `--roi`）；ROI 三处必须一致
2. **输出全黑或全白** → 多半是 `thresh_mode` 或 `thresh_offset` 不对；注意 `thresh_offset` 是**有符号**的
3. **PC 上跑 golden 报错** → 需要 `numpy`；如需读图片还要 `opencv-python` 或 `Pillow`
4. **怀疑数据分布不对** → 拿 `samples/golden_96x96_gray.bin` 做基准，它的 md5 与板上输出相同
