# 样例数据（给 PS / CNN 侧）

> 给 CNN 侧在 **PC 上独立开发调试**用 —— **不需要板子、不需要 overlay**。
> 这正是本项目把边界定在"DDR 里一块 96×96 buffer"的价值。

## 文件

| 文件 | 大小 | 内容 |
|---|---|---|
| `frame_640x480_rgb565.bin` | 614,400 B | **输入**：640×480 RGB565，`uint16` **小端**，行优先 |
| `golden_96x96_gray.bin` | 9,216 B | **期望输出**：96×96 uint8 灰度，行优先 |

```python
import numpy as np
rgb  = np.fromfile('frame_640x480_rgb565.bin', dtype='<u2').reshape(480, 640)
gray = np.fromfile('golden_96x96_gray.bin',     dtype=np.uint8).reshape(96, 96)
```

## ⭐ 这份 golden 是**硬件实测对过**的，不是推算的

`golden_96x96_gray.bin` 与 **PYNQ-Z2 板上输出的 md5 完全相同**：

```
板上输出 md5 : ca5df4fc5fec792ccd4fcb2090bcae25
本文 golden  : ca5df4fc5fec792ccd4fcb2090bcae25
```

对应比特流 `23d25563a4b0362da837544f3a7a80d0`（tag `v0.4-roi-fixed`），
三次不同输入 / 三种 ROI 均逐字节一致。见
[`../docs/board-test-log-2026-09-23.md`](../docs/board-test-log-2026-09-23.md) §9。

## 产生这份输出所用的参数

| 参数 | 值 |
|---|---|
| ROI | `(160, 80) 320×320` |
| `thresh_mode` | 1（二值化） |
| `thresh_offset` | −8 |
| `gain` | 256 |
| `gauss_en` / `sobel_en` / `morph_en` | 1 / 1 / 1 |

处理链顺序：`crop_scale`（含 RGB565→灰度）→ 高斯 → Sobel → 自适应阈值 → 闭运算。

### ⚠⚠ 输出是**二值**的 —— 选 CNN 前必须先确认这件事

| `thresh_mode` | 输出 | 取值种类 | 非零像素 | 用途 |
|---|---|---|---|---|
| **1（默认）** | **二值** | **2**（只有 0 / 255） | 2665 / 9216 | 手形轮廓，抗光照 |
| 0 | 灰度直通 | 99 | 8616 / 9216 | 保留 Sobel 梯度细节 |

**当前默认是 `thresh_mode=1`，所以本目录的 golden 是二值的。**

这个选择直接决定 CNN 能学到什么：

- **二值图**：只有形状/轮廓信息，CNN 学的是手型拓扑。对光照变化**天然鲁棒**。
- **灰度图**：保留边缘强度，信息更多，但依赖 `gain` / 曝光一致性。

> ⚠ **`thresh_mode` 是 PL 侧寄存器（`0x20`），不是纯软件选择** ——
> 改它要重新跑一次板上流程。**建议在 CNN 动手前先把这件事定下来**，
> 否则训练集与推理时的数据分布不一致，模型白练。
>
> 想对比两种模式，用 `--no-thresh` 生成灰度版：
> ```bash
> python host/gesture_golden.py --input frame.bin --roi 160 80 320 320 --no-thresh --out golden_gray.bin
> ```

## 自己造新样本

```bash
# 任意图片 → 640×480 RGB565（cover 缩放 + 居中裁剪，不会拉伸变形）
python host/capture_frame.py --image 你的图.jpg --out frame.bin --png preview.png
#                                        ⚠ 先看 preview.png 对不对

# 算对应的 golden
python host/gesture_golden.py --input frame.bin --roi 160 80 320 320 --out golden.bin
```

> ⚠ **ROI 三处必须一致**：生成输入时用的图 / 板上 `config()` / golden 的 `--roi`。
> 差一个就是另一个答案，对拍全废。

## 接口要点（PS 侧写驱动时看 `../docs/architecture-contract.md` §3）

- 输出 buffer：**9216 字节、4 字节对齐**的物理地址
- 格式：`uint8` 灰度，0–255，**不做归一化**（归一化由 CNN 侧负责）
- 执行顺序：**先武装 S2MM → 再启 MM2S → 最后 `ap_start`**（反了会丢数据）
- 状态位在 `CTRL(0x00)` 的 bit1（`ap_done`），**不在 `0x04`**（那是 `GIE`）
