# 板上常用操作速查

> **面向**：在 PYNQ 板上跑实验的人
> **前置**：板子已开机、网线已通、能开 `http://192.168.2.99:9090`
> **日期**：2026-09-25

本文件只讲**日常操作**。上电、烧镜像、排 `sudo` 环境坑见
[`board-bringup-guide.md`](board-bringup-guide.md)；算法/架构见
[`architecture-contract.md`](architecture-contract.md)。

---

## 一、PC → 板子 传文件

### 1.1 命令在哪边跑（最容易搞错的一条）

| 在哪跑 | 提示符长这样 | 干什么 |
|---|---|---|
| **PC**（Git Bash / PowerShell） | `xiaomu@DESKTOP-…` | `scp`、`ssh`、`python host/*.py` |
| **板子**（Jupyter / SSH） | `xilinx@pynq` | 加载 overlay、跑一帧 |

> ⚠ **在板子上敲 `scp` 会报源路径找不到** —— 因为源文件在 PC 上。
> 反过来在 PC 上 `from pynq import Overlay` 也必然失败（PC 没装 PYNQ）。

### 1.2 scp（本项目最常用）

```bash
# 在 PC 上、Git Bash 里
BOARD=xilinx@192.168.2.99

# 单个文件
scp gesture_system.bit  $BOARD:/home/xilinx/
scp frame.bin           $BOARD:/home/xilinx/

# 多个文件一起
scp frame.bin golden.bin $BOARD:/home/xilinx/

# 整个目录（-r）
scp -r host/ $BOARD:/home/xilinx/

# 从板子拉回来（反过来写就行）
scp $BOARD:/home/xilinx/hw_out.bin ./
```

> ⚠ **`.bit` 与 `.hwh` 必须同时传**（见下文 §四）。只传一个时 PYNQ
> **不报错**，只会「只认出 `default` 一个 IP」，很难往回追。

### 1.2.1 ⭐ 用 `host/push.py` 省掉手敲（推荐）

`scp` 能传，但**图片**每次要记三步：先转 `.bin`、再算 golden、还要保证
ROI 三处一致。`push.py` 把这三件事一起做了：

```bash
# 一张图片 → 自动转 RGB565 + 自动算配套 golden + 传 + 核 md5
python host/push.py 手.jpg

# 指定 ROI（默认用 auto_roi 自动估算）
python host/push.py 手.jpg --roi 160 80 320 320

# 推任意文件（裸传，不转换）
python host/push.py gesture_system.bit gesture_system.hwh

# 推整个目录
python host/push.py -r host/

# 只看会做什么，不实际传
python host/push.py 手.jpg --dry-run
```

它会：

| 做 | 为什么 |
|---|---|
| 图片自动转 640×480 RGB565 | ⚠ 板子**不认 jpg/png**，忘了转传上去也不报格式错 |
| 自动算配套 golden | 只传输入不传 golden，到板上没法对拍 |
| **传完核 md5** | 传了一半 / 传了旧文件，板上跑出旧结果，现象极难追 |
| 传完清 `__pycache__` | 脚本改了但板上是旧字节码 —— 踩过的坑 |

> ⚠ **需要 ssh 免密**。提示要密码时先在 PC 上跑一次
> `ssh-copy-id xilinx@192.168.2.99`。
> 脚本刻意**不处理交互式密码** —— 那就不叫一键了。

> ⚠ 它**不重新实现** RGB565 转换和 golden，而是 import
> `capture_frame.py` / 调 `gesture_golden.py`。
> 这个项目在「多份实现抄同一套错」上栽过一次（`crop_scale` 固定步长，
> HLS/C++/Python 三份一起错），所以**转换只有一份实现**。

### 1.3 另外三种传法（按省事程度排）

| 方式 | 怎么做 | 适合 |
|---|---|---|
| **Jupyter 网页上传** | 浏览器开 `http://192.168.2.99:9090` → 文件树 → 左上 **Upload** 按钮 | 小文件、懒得记命令。**最省事** |
| **MobaXterm SFTP** | 左侧文件树直接拖拽 | 经常传、喜欢图形界面 |
| **HTTP 服务** | PC 上 `python -m http.server 8000`，板上 `wget http://<PC的IP>:8000/文件名` | 大文件、或板子要主动拉 |

> ⚠ 用 HTTP 那招时，PC 的 IP **要写板子能路由到的那块网卡**的地址
> （本机是 `以太网 2` 那块，不是 WLAN，也不是 VMware 虚拟网卡）。

### 1.4 传完必做：核 md5

```bash
# PC 侧
md5sum gesture_system.bit

# 板子（Jupyter 里）
!md5sum /home/xilinx/gesture_system.bit
```

**两边一致才算传成功。** 2026-09-23 就因为板上留着旧 bit，
排查绕了一大圈 —— 见 `../docs/board-test-log-2026-09-23.md`。

---

## 二、传「图片」的完整流程

### ⚠ 先搞清楚最重要的一点

**板子不认 jpg / png。** PL 链路的输入契约只有一条：

> **DDR 里一块 640×480 RGB565 的裸 buffer = 614400 字节，无文件头。**

所以「传图片」= **先在 PC 上把图片转成 `.bin`，再传 `.bin`**。

### 2.1 ⭐ 一条命令搞定（推荐）

```bash
python host/push.py 你的图.jpg
#   → 自动转 RGB565 .bin、自动算配套 golden、传上去、核 md5
#   → 想指定 ROI：加 --roi 160 80 320 320
```

### 2.2 手动三步（想知道每一步在干什么时）

```bash
# ① 在 PC 上：任意图片 → 640×480 RGB565（cover 缩放 + 居中裁剪，不会拉伸变形）
python host/capture_frame.py --image 你的图.jpg --out frame.bin --png preview.png

#    ⚠ 先打开 preview.png 看一眼 —— 变形/裁错在这一步就能发现，
#      传上去再发现就得来回折腾

# ② 在 PC 上：算出这张图对应的 golden（用于对拍）
python host/gesture_golden.py --input frame.bin --roi 160 80 320 320 --out golden.bin

# ③ 传到板子
scp frame.bin golden.bin $BOARD:/home/xilinx/
```

### 2.3 ⚠⚠ ROI 三处必须一致

这是最容易出错、且错了以后**结果全废但不会报错**的地方：

| 位置 | 在哪设 |
|---|---|
| 生成 golden 时 | `gesture_golden.py --roi 160 80 320 320` |
| 板上配置时 | `g.config(roi_x=160, roi_y=80, roi_w=320, roi_h=320)` |
| 生成 frame 时所用图 | `capture_frame.py` 用的原图 |

**差一个数就是另一个答案。** 改 ROI 就必须重算 golden。

### 2.4 从摄像头录一帧（可选，需要 USB 摄像头）

```bash
# PC 上（有 UVC 摄像头）
python host/capture_frame.py --camera 0 --out frame.bin

# 或板上直接录（PYNQ 的 USB Host 口）
%run /home/xilinx/capture_frame.py
```

---

## 三、板上常用 Python（Jupyter 单元格）

### 3.0 ⚠ 必须先切工作目录

Jupyter 的默认工作目录是 `~/jupyter_notebooks`，**不是 `~/`** ——
不切的话 `from gesture_overlay import ...` 直接 `ModuleNotFoundError`。

```python
import os, sys
os.chdir('/home/xilinx')
sys.path.insert(0, '/home/xilinx')
import numpy as np
from gesture_overlay import GesturePipeline
```

> ⚠ Jupyter 的内核**本身就是 root**，所以那套
> `sudo -E /usr/local/share/pynq-venv/bin/python3 ...` 的讲究
> **只在 SSH 下才需要**，在 notebook 里不要加。

### 3.1 标准四步：加载 → 分配 → 配置 → 跑

```python
# ① 加载 overlay + 认 IP（顺带完成 bit 下载）
g = GesturePipeline(bitfile='/home/xilinx/gesture_system.bit')
g.print_info()          # 应看到 3 个 IP：preproc / dma_in / dma_out

# ② 分配 DMA 缓冲
#    ⚠ 必须在 ① 之后 —— 此版 PYNQ 要求先下载过 overlay，allocate() 才能用
g.setup_dma()

# ③ 配置参数
g.config(roi_x=160, roi_y=80, roi_w=320, roi_h=320)
# 其它可选：thresh_mode / thresh_offset / gain / gauss_en / sobel_en / morph_en

# ④ 跑一帧
g.run_once()
out = g.get_result()    # (96, 96) uint8
```

### 3.2 喂自己的图

```python
img = np.fromfile('/home/xilinx/frame.bin', dtype=np.uint8)   # 614400 字节
print(img.size)                     # 必须是 614400

g.in_buf[:] = img
g.in_buf.flush()                    # ⚠⚠ 别省 —— 不 flush 时 DMA 搬的是旧数据
g.run_once()
out = g.get_result()
```

### 3.3 显示与查看

```python
g.show()                            # 画出 96×96 结果
print(out.shape, out.min(), out.max())
print('非零 %d/9216' % int((out != 0).sum()))
```

### 3.4 与 golden 对拍（最硬的验收）

```python
gd = np.fromfile('/home/xilinx/golden.bin', dtype=np.uint8).reshape(96, 96)
d  = np.flatnonzero(out != gd)
print('%d/%d 不一致 → %.1f%% 一致'
      % (len(d), gd.size, 100.0 * (gd.size - len(d)) / gd.size))
```

**判定**：`0/9216 不一致` 才算过。

### 3.5 保存结果

```python
out.tofile('/home/xilinx/hw_out.bin')
```

### 3.6 一键入口 / 自检脚本

```python
# 日常"跑一次看一眼"（交互式菜单，列出可用输入让你选）
%run /home/xilinx/run.py

# 完整自检（DDR + 预处理链，带断言与判定）
!python3 /home/xilinx/bringup_check.py --bit /home/xilinx/gesture_system.bit
```

> ⚠ 在 notebook 里直接调 `bringup_check.main()` 会因为
> argparse 去解析 Jupyter 自己的 `sys.argv` 而报错。
> 要用 `!python3 …`，或先 `sys.argv = ['bringup_check', '--bit', '…']`。

### 3.7 常用 shell 命令（`!` 前缀）

```python
!ls -l /home/xilinx/*.bin
!md5sum /home/xilinx/gesture_system.bit
!df -h                              # 磁盘
!free -m                            # 内存（板上只有 ~493 MB）
!ls /home/xilinx/__pycache__        # 怀疑加载了旧代码时看这里
```

---

## 四、换比特流时必做的三件事

```python
# ① 传新文件（PC 上，.bit 与 .hwh 必须一起）
# scp gesture_system.bit gesture_system.hwh $BOARD:/home/xilinx/

# ② 清 __pycache__（否则可能加载旧的 gesture_overlay 字节码）
!sudo rm -rf /home/xilinx/__pycache__

# ③ 重启 Jupyter 内核
#    ⚠ 必须 —— PYNQ 的 overlay 不会因为你换了文件就失效，
#      同一个内核里重新 GesturePipeline() 未必真加载了新 bit
```

**然后重跑一遍 `bringup_check.py`。** 新比特流**必须重新验证**，
不能假定与旧版等价 —— Vivado 每次布线结果不同
（见 `../../board_test/verified_v0.4/README.md`）。

---

## 五、坑清单（都实际踩过）

| 现象 | 原因 | 处置 |
|---|---|---|
| `ModuleNotFoundError: gesture_overlay` | Jupyter 工作目录不是 `/home/xilinx` | 见 §3.0 |
| `RuntimeError: Overlay is not downloaded` | 没加载 overlay 就 `allocate()` | 先建 `GesturePipeline`，再 `setup_dma()` |
| `RuntimeError: No Devices Found` | 用了系统 python 而非 PYNQ venv | 见 `board-bringup-guide.md` §3.4 |
| 输出全黑且不报错 | `thresh_offset` 写成 8 位补码（−8→248） | 检查 `config()` 的回读断言 |
| 只认出 `default` 一个 IP | `.bit` 与 `.hwh` 不同名/不配套 | 见 `board-bringup-guide.md` §4.0 |
| 认到的 IP 地址全是 `0x00000000` | `print_info` 曾用 PYNQ 2.x 的 `base_addr` 键 | 已修（改用 `phys_addr`）；拉最新 `gesture_overlay.py` |
| 改了代码但行为没变 | 加载了 `__pycache__` 里的旧字节码 | 清 `__pycache__` + 重启内核 |
| 对拍全不一致 | ROI 三处不一致 | 见 §2.3 |
| DMA 只搬了前 16 KB | `C_SG_LENGTH_WIDTH` 是默认的 14 位 | 重建（`rebuild_all.sh` 会硬性校验 24） |

---

## 六、找到板子的 IP

`192.168.2.99` 是常用的那个，但**不保证**（PYNQ 也可能走 DHCP）。
从串口启动日志里抠 IP：

```bash
python host/pynq_serial.py          # 在 PC 上跑，会自动打印 http://<ip>:9090
```

详见 `board-bringup-guide.md` §3.3。
