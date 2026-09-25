#!/usr/bin/env python3
"""
gesture_overlay.py —— 手势识别 PL 流水线的 PYNQ 驱动

在 PYNQ-Z2 的 Jupyter / Python 里运行，加载 bitstream 并驱动整条链：

    OV5640 ─DVP─► dvp_capture ─AXIS─► VDMA S2MM ─► DDR(帧缓存)
                                                        │
                                     AXI DMA MM2S ◄─────┘
                                            │
                                     gesture_preproc ──► 96x96 灰度
                                            │
                                     AXI DMA S2MM ──► DDR(96x96) ──► CNN

=====================================================================
  使用前必须知道的三件事
=====================================================================
1. **地址不硬编码。** 所有基地址从 overlay 的 `ip_dict` 读 ——
   重新综合后地址可能变，硬编码必然出错。

2. **⚠ `ignore_version=True` 可能需要。**
   PYNQ 镜像自带的是某个 Vivado 版本生成的库，与本项目用的
   Vivado 2025.2 不一致时，`Overlay()` 会报版本错误。
   先试不加参数的；报错再加。

3. **⚠ 摄像头能否出图，上板前无法确认。**
   `sccb_master` 的寄存器表（`rtl/ov5640_regs.v`）已换成 **250 条真实配置**
   （正点原子来源，固化为 640×480 RGB565），**结构**已由 csim/iverilog 验证过
   （拼接顺序、表长 vs N_REGS），但**内容正确性未上板验证**。

   所以：**先跑不依赖摄像头的验证**（见
   `docs/board-bringup-guide.md` §5 的 ②③，已脚本化为 `host/bringup_check.py`），
   把数据通路确认下来，再插摄像头。

=====================================================================
  快速上手
=====================================================================
    from gesture_overlay import GesturePipeline
    g = GesturePipeline()
    g.print_info()          # 先看 IP 认出来没有
    g.setup_dma()           # 配 DMA 缓冲
    g.run_once()            # 跑一帧
    g.show()                # 看结果（Jupyter 里出图）
"""

import time

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    # ⚠ numpy 软导入 —— 与下面的 pynq 同一个理由。
    #
    #   本模块里**与硬件无关**的那部分（`_as_i32` / `check_config` /
    #   寄存器与 DMA 偏移常量）根本不碰数组，是纯逻辑。
    #   把这些也绑死在 numpy 上，会让"不需要板子就能跑的自检"
    #   凭空多一个它并不使用的依赖 —— CI 上就是这么红的。
    #
    #   真正用到 numpy 的地方（`get_result` / `fill_test_pattern`）
    #   在板上跑，而 PYNQ 镜像自带 numpy，不会走到这个分支。
    _NUMPY = False

def _find_bitfile():
    """找同目录下的 .bit —— 供 `GesturePipeline()` 无参调用时用。

    ⚠ 为什么要自己找：`pynq.Overlay` 的构造器**要求显式传路径**
      （见 `GesturePipeline.__init__` 里的说明）。
      在 notebook 里 `Overlay()` 能自动找同名文件，那是 PYNQ 的
      语法糖，类构造器没有这个行为。

    优先认本项目约定的名字 `gesture_system.bit`；找不到就看看目录里
    唯一的 .bit（只有一块板子 / 一份 overlay 时省事）。
    多个候选且都不叫约定名时**报错而不是瞎猜** —— 猜错等于加载了
    别的设计，而现象会是"IP 找不到"，很难往回查到是选错文件。
    """
    import os
    here = os.path.dirname(os.path.abspath(__file__))

    # 1) 约定名（优先当前工作目录 —— notebook 通常就是在这里跑的）
    for d in (os.getcwd(), here):
        p = os.path.join(d, 'gesture_system.bit')
        if os.path.isfile(p):
            return p

    # 2) 退化：目录里唯一的 .bit
    for d in (os.getcwd(), here):
        try:
            cands = sorted(f for f in os.listdir(d) if f.endswith('.bit'))
        except OSError:
            cands = []
        if len(cands) == 1:
            return os.path.join(d, cands[0])
        if len(cands) > 1:
            raise FileNotFoundError(
                "当前目录有多个 .bit，无法确定用哪个：%s\n"
                "  请显式指定： GesturePipeline(bitfile='.../gesture_system.bit')\n"
                "  （目录：%s）" % (cands, d))

    raise FileNotFoundError(
        "找不到 .bit。请把 gesture_system.bit 传到板上，或显式指定：\n"
        "  GesturePipeline(bitfile='/home/xilinx/gesture_system.bit')")


try:
    from pynq import Overlay, allocate
    _PYNQ = True
except ImportError:
    # 允许在 PC 上 import 本模块做静态检查（_PYNQ=False 时不加载硬件）
    _PYNQ = False


def _need_numpy(what):
    """用到 numpy 的功能在缺 numpy 时给出明确报错，而不是 NameError"""
    if not _NUMPY:
        raise RuntimeError(
            "%s 需要 numpy，但当前环境没有。\n"
            "  在 PYNQ 板上这不该发生（镜像自带 numpy）。\n"
            "  在 PC 上只跑离线自检的话不需要它 —— "
            "见 host/test_overlay_offline.py" % what)


# =====================================================================
#  与 src_hls/gesture_preproc.h 必须一致的常量
# =====================================================================
IN_WIDTH  = 640
IN_HEIGHT = 480
IN_BYTES  = IN_WIDTH * IN_HEIGHT * 2      # RGB565 = 2 字节/像素

OUT_SIZE  = 96
OUT_PIXELS = OUT_SIZE * OUT_SIZE          # 9216
OUT_BYTES = OUT_PIXELS                    # uint8

# gesture_preproc 的 AXI-Lite 寄存器偏移
# ⚠ 取自 Vitis 生成的官方头文件 xgesture_preproc_hw.h，不是凭记忆
REG_CTRL          = 0x00
REG_GIE           = 0x04   # ⚠ 中断使能，不是状态！
REG_IER           = 0x08
REG_ISR           = 0x0C
REG_WIDTH         = 0x10
REG_HEIGHT        = 0x18
REG_THRESH_MODE   = 0x20
REG_THRESH_OFFSET = 0x28
REG_GAUSS_EN      = 0x30
REG_SOBEL_EN      = 0x38
REG_MORPH_EN      = 0x40
REG_GAIN          = 0x48
REG_ROI_X         = 0x50
REG_ROI_Y         = 0x58
REG_ROI_W         = 0x60
REG_ROI_H         = 0x68

# CTRL 位 —— 取自官方 xgesture_preproc.c
CTRL_AP_START  = 0x01   # bit0
CTRL_AP_DONE   = 0x02   # bit1
CTRL_AP_IDLE   = 0x04   # bit2
CTRL_AP_READY  = 0x08   # bit3

# AXI DMA 的寄存器偏移（PG021 固定布局）
DMA_MM2S_DMACR   = 0x00
DMA_MM2S_DMASR   = 0x04
DMA_MM2S_SRCADDR = 0x18
DMA_MM2S_LENGTH  = 0x28

DMA_S2MM_DMACR   = 0x30
DMA_S2MM_DMASR   = 0x34
DMA_S2MM_DSTADDR = 0x48
DMA_S2MM_LENGTH  = 0x58

DMA_CR_RUNSTOP = 0x0001
DMA_CR_RESET   = 0x0004
DMA_SR_HALTED  = 0x0001
DMA_SR_IDLE    = 0x0002


def _dma_sr_str(sr):
    """把 AXI DMA 的 DMASR 拆成可读文字（错误位优先）

    ⚠ 位定义来自 PG021。**bit1 是 IDLE（1=空闲），不是完成位** ——
      2026-09-21 的诊断脚本就是把它当完成位用，导致结论整个跑偏。
      **完成**是 bit12 `IOC_Irq`。

    仅用于**超时时的现场快照**，正常运行路径不调用。
    """
    bits = [
        (0x0001, 'HALTED'), (0x0002, 'IDLE'), (0x0010, 'DMAIntErr'),
        (0x0020, 'DMASlvErr'), (0x0040, 'DMADecErr'),
        (0x1000, 'IOC_Irq'), (0x2000, 'Dly_Irq'), (0x4000, 'Err_Irq'),
    ]
    if sr == 0xFFFFFFFF:
        return '!! 读回全 1 —— 该地址可能不在总线上'
    hit = [n for m, n in bits if sr & m]
    return (', '.join(hit) if hit else '（无标志位）') + ' [0x%08X]' % sr

# ⚠⚠ 有符号参数写进 32 位 AXI-Lite 寄存器时的**位宽**问题（2026-09-18 修的 bug）
#
#   `thresh_offset` 在 HLS 侧是 `int`（s_axilite，32 位）。所以：
#
#     要传 -8  →  写 0xFFFFFFF8  →  IP 按 int32 读回 -8   ✅
#     不能写 0x000000F8，那 IP 读回来是 **+248**，阈值被抬高，
#     输出**全黑** —— 而且 ap_done 照常置位，**不报任何错**。
#
#   本文件原先写的是 `-8 & 0xFF`（8 位补码），正是后一种。
#   讽刺的是那行注释写的是"有符号转补码"—— 思路对，**位宽错**。
#
#   同一个坑，C 驱动 `sw/preproc_driver.c` 做对了：
#       (uint32_t)(int32_t)dev->thresh_offset   →  0xFFFFFFF8
#   它的主机自检 `main_preproc.c` 第 4 项专门验这个，
#   而且注释里明确写了"直接 (uint32_t)(-8) 会变成 0xFFFFFFF8，
#   IP 侧当成巨大的正数……表现为输出全黑或全白"。
#   **C 侧写对了却少有人注意，Python 侧写错了却没人查 —— 两边都要核。**
DEFAULT_THRESH_MODE   = 1
DEFAULT_THRESH_OFFSET = -8          # ⚠ 保持有符号！写入时按 32 位补码转换
DEFAULT_GAIN          = 256


def _as_i32(v):
    """把有符号整数转成 32 位补码（写入 s_axilite 寄存器的正确方式）。

    ⚠ 不要用 `v & 0xFF` —— AXI-Lite 寄存器是 32 位，
      8 位补码会让 IP 把负数读成大的正数。
    """
    return v & 0xFFFFFFFF


def check_config(thresh_mode, thresh_offset, gain,
                 roi_x, roi_y, roi_w, roi_h):
    """参数检查 + ROI 居中补齐。

    **纯函数，不碰硬件** —— 所以可以在 PC 上离线测
    （见 `host/test_overlay_offline.py`）。
    这一点是有意的：这些检查是"写进硬件之前"的最后一道闸，
    不该因为"没板子就跑不了"而失去回归覆盖。

    ⚠ 各条判据与 `sw/preproc_driver.c` 的 `preproc_config()` 逐条对齐。
      改这里就要同步改那里，反之亦然。

    返回补齐后的 dict；非法值抛 ValueError。
    """
    if thresh_mode not in (0, 1):
        raise ValueError("thresh_mode 只能是 0(灰度直通) 或 1(二值化)，收到 %r"
                         % (thresh_mode,))
    if not (-128 <= thresh_offset <= 127):
        raise ValueError("thresh_offset 必须在 [-128, 127]，收到 %r"
                         % (thresh_offset,))
    if gain < 0:
        raise ValueError("gain 不能为负，收到 %r" % (gain,))
    if roi_w <= 0 or roi_h <= 0:
        raise ValueError("ROI 宽高必须为正，收到 %dx%d" % (roi_w, roi_h))

    # ⚠ ROI 必须 ≥ 96×96（= 输出尺寸 OUT_SIZE）。
    #   缩放采用**按比例分配**，隐含除数 roi_w/96、roi_h/96 ——
    #   ROI 小于输出尺寸会除零。PL 顶层 gesture_preproc 与
    #   sw/preproc_driver.c 的 check_roi 都已加这条，三处必须一致。
    #   （旧实现是固定步长 + 补零，允许更小 ROI，但输出大部分恒为零。）
    if roi_w < OUT_SIZE or roi_h < OUT_SIZE:
        raise ValueError("ROI 必须至少 %dx%d（按比例分配隐含除数 roi_w/%d），"
                         "收到 %dx%d" % (OUT_SIZE, OUT_SIZE, OUT_SIZE, roi_w, roi_h))

    if roi_x is None: roi_x = (IN_WIDTH  - roi_w) // 2
    if roi_y is None: roi_y = (IN_HEIGHT - roi_h) // 2

    if roi_x < 0 or roi_y < 0 or roi_x + roi_w > IN_WIDTH or roi_y + roi_h > IN_HEIGHT:
        raise ValueError("ROI 越界：(%d,%d) %dx%d 超出 %dx%d"
                         % (roi_x, roi_y, roi_w, roi_h, IN_WIDTH, IN_HEIGHT))

    return {'thresh_mode': thresh_mode, 'thresh_offset': thresh_offset,
            'gain': gain,
            'roi_x': roi_x, 'roi_y': roi_y, 'roi_w': roi_w, 'roi_h': roi_h}


class GesturePipeline(object):
    """手势识别流水线的 PYNQ 驱动。

    ⚠ 所有基地址从 overlay.ip_dict 读，不硬编码。
    """

    def __init__(self, bitfile=None, ignore_version=False):
        if not _PYNQ:
            raise RuntimeError(
                "pynq 未安装 —— 本模块只能在 PYNQ-Z2 的板载 Python 里运行。\n"
                "在 PC 上做静态检查时用 import 即可，但不要实例化。")

        # ⚠⚠ PYNQ 3.x 的 Overlay 签名是
        #       Overlay(bitfile_name, dtbo=None, download=True, ...)
        #   —— `bitfile_name` 是**必填位置参数**（没有默认值）。
        #   写 `Overlay(**kw)` 会直接抛
        #       TypeError: __init__() missing 1 required positional
        #                  argument: 'bitfile_name'
        #   **不会**自动去找同名 .bit —— "无参调用自动找文件"那是
        #   `pynq.Overlay` 在 notebook 里的语法糖，类构造器没有这个行为。
        #   所以必须自己把路径解析出来再传进去。
        if bitfile is None:
            bitfile = _find_bitfile()
        kw = {}
        if ignore_version:
            kw['ignore_version'] = True
        self.ol = Overlay(bitfile, **kw)

        self.ip = {}          # 名字 -> MMIO
        self._find_ips()

        self.in_buf  = None   # 输入帧缓冲（DDR）
        self.out_buf = None   # 输出 96x96 缓冲（DDR）

    # -----------------------------------------------------------------
    def _find_ips(self):
        """从 overlay 里认出需要的 IP。

        ⚠ 不硬编码实例名 —— `.hwh` 里的名字可能与 BD 里的不同
          （BD 里叫 `dma_in`，`.hwh` 里可能是
           `bd_video_i/dma_in` 这种层次路径）。

        ⚠⚠ 匹配逻辑要当心两点：
          1. **运算符优先级**：`A and B or C` 是 `(A and B) or C`，
             不是 `A and (B or C)`。早期写成
                if pat in name and 'dma_' in name or (...)
            结果 `dma_in` 和 `dma_out` 互相误匹配。
          2. **一个模式可能命中多个 IP**，这时要报歧义而不是
             随便取第一个 —— 取错的话表现为"配了 A 结果 B 动了"，
             极难查。
        """
        d = self.ol.ip_dict

        # 每个角色：(名字里的特征, 期望的 IP 类型)
        want = {
            'preproc': ('gesture_preproc', 'gesture_preproc'),
            'dma_in':  ('dma_in',          'axi_dma'),
            'dma_out': ('dma_out',         'axi_dma'),
        }

        for key, (name_pat, type_pat) in want.items():
            hits = []
            for name, info in d.items():
                if name_pat in name and type_pat in str(info.get('type', '')):
                    hits.append(name)

            if len(hits) == 0:
                continue          # 留给下面的报错统一处理
            if len(hits) > 1:
                raise RuntimeError(
                    "IP 匹配有歧义：'%s' 同时命中 %s\n"
                    "  请检查 .hwh 里的实例名，或改用显式指定。"
                    % (name_pat, hits))
            self.ip[key] = self.ol.__getattr__(hits[0])

        missing = [k for k in want if k not in self.ip]
        if missing:
            print("=== overlay 里实际的 IP ===")
            for name, info in sorted(d.items()):
                print("  %-40s type=%s" % (name, info.get('type', '?')))
            raise RuntimeError(
                "overlay 里没找到这些 IP: %s\n"
                "（上面列出了实际有的，对照调整匹配特征）" % missing)

    # -----------------------------------------------------------------
    def print_info(self):
        """打印 overlay 里的 IP 与地址 —— 排查用"""
        print("=== overlay 里的 IP ===")
        for name, info in sorted(self.ol.ip_dict.items()):
            # ⚠ 键名是 `phys_addr`，**不是** `base_addr`。
            #   `base_addr` 是 PYNQ 2.x 的键，3.x 已改名 —— 写错不报错，
            #   只是 `.get()` 取不到就回落成 0，所有地址都打印成 0x00000000，
            #   排查时会误判成"IP 没映射到地址空间"，方向完全错。
            #
            # ⚠⚠ 这个坑本文件**已经踩过一次**：下面 `_find_ips` 附近
            #   2026-09-21 就记录过「`base_addr` 键不存在，误导过一轮」，
            #   但当时只改了那一处，`print_info()` 这里漏了。
            #   2026-09-25 上板（PYNQ 3.0.1）再踩一次才发现。
            #   —— 同一条知识只落在一处，另一处照样错。
            #   改这类"键名 / 字符串常量"的坑时，务必全文件搜一遍。
            print("  %-40s @ 0x%08X  (%s)" % (
                name, info.get('phys_addr', 0), info.get('type', '?')))
        print()
        print("=== 本脚本认到的 ===")
        for k, v in self.ip.items():
            print("  %-10s -> %s" % (k, v))

    # -----------------------------------------------------------------
    def setup_dma(self, in_bytes=IN_BYTES, out_bytes=OUT_BYTES):
        """分配 DMA 缓冲。

        ⚠ `pynq.allocate` 出来的 buffer 是 **cache 一致**的
          （分配时就把页标记成不可缓存），**不需要**像裸机那样
          手动 `Xil_DCacheFlushRange` —— 这是 PYNQ 相对裸机的最大便利。

        ⚠⚠ 但**不要把"cache 一致"误读成"可以不管 flush/invalidate"** ——
          这是本文件一度写错的地方。实际情况是：

            · **buffers 本身是 cache 一致的**，DMA 看到的就是内存里的值；
            · 但 `.flush()` / `.invalidate()` 在 PYNQ 里仍是**必要的**，
              因为 CPU 侧可能持有**已缓存的行** ——
              `.flush()` 把 CPU 改的推下去，`.invalidate()` 把 DMA 写的拉上来。

          所以：**填完输入要 `in_buf.flush()`，读完输出要 `out_buf.invalidate()`**。
          漏刷的症状是**静默的**（ap_done 照常置位，只是数据是旧的），
          与裸机漏刷 `Xil_DCacheFlushRange` 表现完全一样。
          本文件现在 `run_once()` 里无条件刷输入，就是为了堵这个。
        """
        # 输入：614400 字节。pynq 的 allocate 有对齐要求，多分配一点
        _need_numpy("setup_dma()")
        self.in_buf  = allocate(shape=(in_bytes,),  dtype=np.uint8)
        self.out_buf = allocate(shape=(out_bytes,), dtype=np.uint8)

        # 输出缓冲清零，便于判断"有没有跑出东西"
        self.out_buf[:] = 0
        self.out_buf.flush()

        print("DMA 缓冲已分配：")
        print("  输入 %d 字节 @ 物理地址 0x%08X" % (in_bytes, self.in_buf.physical_address))
        print("  输出 %d 字节 @ 物理地址 0x%08X" % (out_bytes, self.out_buf.physical_address))

    # -----------------------------------------------------------------
    def config(self, thresh_mode=DEFAULT_THRESH_MODE,
               thresh_offset=DEFAULT_THRESH_OFFSET,
               gauss_en=1, sobel_en=1, morph_en=1,
               gain=DEFAULT_GAIN,
               roi_x=None, roi_y=None, roi_w=320, roi_h=320):
        """配置预处理参数

        ⚠ 参数检查走 `check_config()`（纯函数，可离线测）——
          它必须与 `sw/preproc_driver.c` 的 `preproc_config()` 对齐：
          两个驱动要挡同样的东西，否则会出现"Python 能跑、C 跑不了"
          （或反过来）这种最难查的不一致。
        """
        cfg = check_config(thresh_mode, thresh_offset, gain,
                           roi_x, roi_y, roi_w, roi_h)
        thresh_mode   = cfg['thresh_mode']
        thresh_offset = cfg['thresh_offset']
        roi_x, roi_y  = cfg['roi_x'], cfg['roi_y']
        roi_w, roi_h  = cfg['roi_w'], cfg['roi_h']

        p = self.ip['preproc']
        p.write(REG_WIDTH,  IN_WIDTH)
        p.write(REG_HEIGHT, IN_HEIGHT)
        p.write(REG_THRESH_MODE,   thresh_mode)
        # ⚠⚠ 32 位补码，不是 8 位 —— 见文件头 _as_i32 的说明。
        #    写成 `& 0xFF` 会让 -8 变成 +248，输出全黑且不报错。
        p.write(REG_THRESH_OFFSET, _as_i32(thresh_offset))
        p.write(REG_GAUSS_EN, gauss_en)
        p.write(REG_SOBEL_EN, sobel_en)
        p.write(REG_MORPH_EN, morph_en)
        p.write(REG_GAIN,     gain)
        p.write(REG_ROI_X, roi_x)
        p.write(REG_ROI_Y, roi_y)
        p.write(REG_ROI_W, roi_w)
        p.write(REG_ROI_H, roi_h)

        # ---- 回读断言：寄存器写没写进去，别等结果不对再查 ----
        # ⚠ 本项目在 Tcl 侧靠回读断言拦下过两次错误，这里同理。
        got_off = p.read(REG_THRESH_OFFSET)
        if got_off > 0x7FFFFFFF:            # 转成有符号再看
            got_off -= 0x100000000
        if got_off != thresh_offset:
            raise RuntimeError(
                "thresh_offset 回读不符：写入 %d，读回 %d\n"
                "  （若读回的是 248 而不是 -8，说明补码位宽写错了）"
                % (thresh_offset, got_off))

        print("已配置: ROI=(%d,%d) %dx%d, gain=%d, thresh_off=%d"
              % (roi_x, roi_y, roi_w, roi_h, gain, thresh_offset))

    # -----------------------------------------------------------------
    def _soft_reset_dma(self, base, sr_offs, cr_off, timeout=1.0):
        """软复位一个 DMA 通道

        `sr_offs` 是**候选偏移的列表**。

        ⚠ 背景：BD 里 `dma_in` 是纯 MM2S、`dma_out` 是纯 S2MM
          （`bd_video.tcl` 的 `c_include_mm2s` / `c_include_s2mm`），
          调用点按角色传的偏移**本来就是对的**。

          但 `.hwh` 对**两个 IP 都列出了 MM2S 与 S2MM 两套寄存器**
          （那是 IP 的完整寄存器表，不反映实际使能的通道）。
          万一哪天通道配错、或 Pynq 的 `DMA` 对象把通道认反，
          **读错的偏移会返回恒定垃圾值** → `DMA_SR_HALTED` 判据失效 →
          **复位看似成功、实则没发生**，最后表现为 `ap_done` 超时。
          这个故障模式**静默**，最难查。

        → 所以同时盯所有候选，**哪个先出现 `HALTED` 就认哪个**。
          正确配置下行为不变，配错时也能自愈，且不必先知道答案。
        """
        base.write(cr_off, DMA_CR_RESET)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if any(base.read(o) & DMA_SR_HALTED for o in sr_offs):
                return                      # 有一个通道报了 HALTED，复位成功
        raise RuntimeError(
            "DMA 复位超时（在偏移 %s 上都未见 HALTED）"
            % ", ".join("0x%02X" % o for o in sr_offs))

    def run_once(self, timeout=5.0):
        """跑一帧：DDR(640x480) -> 96x96 灰度

        ⚠⚠ 顺序不能反：
            1. 先武装 dma_out（S2MM）—— 让它准备好接收
            2. 再启动 dma_in（MM2S）—— 开始供数
            3. 最后 ap_start
          反了的话，预处理输出的第一拍没有接收方，数据会丢。

        ⚠ 与裸机 C 驱动（sw/preproc_driver.c）是同一套顺序 ——
          两边改的时候要同步，否则会出现"主机仿真过了但板上不对"。
        """
        din  = self.ip['dma_in']
        dout = self.ip['dma_out']
        p    = self.ip['preproc']

        # 0a. ⚠ 刷输入 buffer 的 cache。
        #     `fill_test_pattern()` 里也刷过，这里再刷一次是**有意冗余** ——
        #     因为它堵住一个很隐蔽的失败模式：**用别的路径填数据时忘了刷**。
        #     （比如 `np.frombuffer(in_buf)[:] = ...`、或从 .bin 读进来。）
        #     漏刷的症状是：**DMA 搬走的是旧数据**，而 ap_done 正常置位、
        #     不报任何错 —— 与"输出全黑"一样属于静默失败。
        #     刷 614 KB 约 1 ms 量级，相比一帧 20+ ms 可以忽略。
        #     ⚠ 与 C 驱动对照：裸机侧必须显式 Xil_DCacheFlushRange，
        #     漏了同样不报错。见 sw/preproc_driver.c。
        self.in_buf.flush()

        # 0b. 复位两个 DMA
        #     ⚠ 传候选列表：正确配置下首项即命中，行为不变；
        #       万一通道认反，也不会静默漏掉（见 _soft_reset_dma 说明）。
        self._soft_reset_dma(din,  [DMA_MM2S_DMASR, DMA_S2MM_DMASR],
                             DMA_MM2S_DMACR)
        self._soft_reset_dma(dout, [DMA_S2MM_DMASR, DMA_MM2S_DMASR],
                             DMA_S2MM_DMACR)

        # 1. ⚠ 先武装 S2MM
        dout.write(DMA_S2MM_DSTADDR, self.out_buf.physical_address)
        dout.write(DMA_S2MM_DMACR, DMA_CR_RUNSTOP)
        dout.write(DMA_S2MM_LENGTH, OUT_BYTES)

        # 2. 再启 MM2S
        din.write(DMA_MM2S_SRCADDR, self.in_buf.physical_address)
        din.write(DMA_MM2S_DMACR, DMA_CR_RUNSTOP)
        din.write(DMA_MM2S_LENGTH, IN_BYTES)

        # ⚠ 启动后**立刻读回 LENGTH** —— 这是关键诊断。
        #
        #   2026-09-21 实测：超时后 dump 里 `dma_in` 的 LENGTH 读回 **8192**，
        #   而写入的是 614400。必须区分两种可能：
        #
        #     (a) 长度没写进去 → DMA 只搬了 8192 或别的量
        #     (b) 写进去了，读回的是**剩余量**（AXI DMA 传输中该寄存器
        #         语义会变）→ 8192 = 没搬完的零头
        #
        #   在这里（刚写完、传输刚开始）读一次，与超时后的值对比即可判定：
        #     · 立刻读 = 614400 且超时后 = 8192  → (b) 剩余量，正常
        #     · 立刻读 = 8192                        → (a) 写没进去 ← 真 bug
        _len_now_din  = din.read(DMA_MM2S_LENGTH)
        _len_now_dout = dout.read(DMA_S2MM_LENGTH)
        print("  启动回读: dma_in LENGTH=%d (写 %d) %s | dma_out LENGTH=%d (写 %d) %s"
              % (_len_now_din, IN_BYTES,
                 'OK' if _len_now_din == IN_BYTES else '⚠ 不符',
                 _len_now_dout, OUT_BYTES,
                 'OK' if _len_now_dout == OUT_BYTES else '⚠ 不符'))

        # 3. 最后 ap_start
        c = p.read(REG_CTRL)
        p.write(REG_CTRL, c | CTRL_AP_START)

        # 4. 轮询 ap_done
        #    ⚠ 状态位在 CTRL(0x00)，不是 0x04（那是 GIE）
        t0 = time.time()
        while not (p.read(REG_CTRL) & CTRL_AP_DONE):
            if time.time() - t0 > timeout:
                # ⚠⚠ 超时现场快照 —— 这是**唯一**能看到"卡在哪"的机会。
                #
                #   2026-09-21 首次上板实测时，这里只打印了四条"查摄像头"
                #   的提示，而 ③ 这一步**根本不接摄像头**（输入来自 DDR）。
                #   结果整场排查都在猜"DMA 到底启动没有"，
                #   却从没在故障发生的那一刻读过它的寄存器。
                #   别人的提示只会把人带向错误方向 —— 换成实际读到的值。
                self._dump_failure(p, din, dout)
                raise RuntimeError(
                    "等 ap_done 超时（%.1fs）。上方已打印故障现场的寄存器快照，"
                    "按那里的判读走。\n"
                    "  ⚠ 本步骤**不接摄像头**，不要往那个方向查。"
                    % timeout)
        dt = time.time() - t0

        # 5. 清 ap_start
        p.write(REG_CTRL, 0)

        # 6. 让 CPU 看到 DMA 写的结果
        #    ⚠ allocate 的 buffer 是 cache 一致的，一般不需要 invalidate，
        #      但显式写一遍无害，且能防"PYNQ 版本差异导致的一致性问题"
        self.out_buf.invalidate()

        print("跑完一帧，耗时 %.3f s" % dt)
        return dt

    # -----------------------------------------------------------------
    def _dump_failure(self, p, din, dout):
        """超时现场快照：把 IP 与两个 DMA 的寄存器全打出来

        **这是排查超时的唯一有效手段** —— 故障现场只有一次，
        把状态记下来，比事后写一堆探针去猜有意义得多。

        ⚠ 只读寄存器，不改任何状态。可以在 `run_once` 失败后直接调用。
        """
        print("\n" + "!" * 69)
        print("  故障现场快照（超时时刻的寄存器）")
        print("!" * 69)

        # ---- 先打物理地址：地址不对的话，后面全是垃圾 ----
        #    ⚠ 用 ip_dict 里的 'phys_addr' 键 —— 2026-09-21 在板上实测确认过
        #      这个版本的 ip_dict 里有它（当时 `print_info` 读的
        #      `base_addr` 键**不存在**，所有地址都打印成 0，误导过一轮）。
        print("\n  [地址]")
        try:
            for name, info in sorted(self.ol.ip_dict.items()):
                pa = info.get('phys_addr')
                print("    %-20s phys = %s"
                      % (name, ('0x%08X' % pa) if pa is not None else '(无 phys_addr 键)'))
        except Exception as e:
            print("    取地址失败: %s" % e)

        # ---- IP ----
        print("\n  [IP gesture_preproc]")
        try:
            c = p.read(REG_CTRL)
            print("    CTRL  = 0x%08X  [ap_start=%d ap_done=%d ap_idle=%d ap_ready=%d]"
                  % (c, c & 1, (c >> 1) & 1, (c >> 2) & 1, (c >> 3) & 1))
            print("    ISR   = 0x%08X   (bit0 是 ap_done 的粘滞标志)"
                  % p.read(REG_ISR))
            print("    参数回读（确认真写进去了，0 值会让 HLS 判非法）：")
            for reg, nm in [(REG_WIDTH, 'width'), (REG_HEIGHT, 'height'),
                            (REG_THRESH_MODE, 'thresh_mode'),
                            (REG_THRESH_OFFSET, 'thresh_offset'),
                            (REG_ROI_X, 'roi_x'), (REG_ROI_Y, 'roi_y'),
                            (REG_ROI_W, 'roi_w'), (REG_ROI_H, 'roi_h')]:
                print("      %-14s = 0x%08X" % (nm, p.read(reg)))
        except Exception as e:
            print("    读 IP 失败: %s" % e)

        # ---- 两个 DMA ----
        print("\n  [DMA]")
        for nm, ip, cr, sr, addr_o, len_o in [
                ('dma_in (MM2S)', din,  DMA_MM2S_DMACR, DMA_MM2S_DMASR,
                 DMA_MM2S_SRCADDR, DMA_MM2S_LENGTH),
                ('dma_out(S2MM)', dout, DMA_S2MM_DMACR, DMA_S2MM_DMASR,
                 DMA_S2MM_DSTADDR, DMA_S2MM_LENGTH)]:
            try:
                crv, srv = ip.read(cr), ip.read(sr)
                print("    %s" % nm)
                print("      DMACR  = 0x%08X  (bit0 RS=%d, 应锁存为 1)"
                      % (crv, crv & 1))
                print("      DMASR  = %s" % _dma_sr_str(srv))
                # ⚠ 地址与长度也要打 —— 只看到 "已武装" 不够，
                #   长度写错的话它会武装上但永远收不满，表现和"没数据"一样。
                print("      ADDR   = 0x%08X" % ip.read(addr_o))
                print("      LENGTH = %d 字节" % ip.read(len_o))
            except Exception as e:
                print("    %s 读失败: %s" % (nm, e))

        # ---- 判读 ----
        print("\n  [判读]")
        print("    · DMACR 的 RS 位没锁存 → 写没生效，AXI-Lite 路径有问题")
        print("    · DMASR 报 HALTED       → DMA 拒绝启动")
        print("    · DMASR 报 IDLE         → 通道空闲")
        print("        - 带 IOC_Irq  → **传输已完成过**（数据搬完了）")
        print("        - 无 IOC_Irq  → 空转，没有数据可搬")
        print("    · DMASR 报 *Err         → 总线访问出错，查 ic_hp3/地址映射")
        print("    · LENGTH 与预期不符     → 长度写错，会武装上但永远收不满")
        print("    · 参数回读为 0          → config() 没生效，HLS 会走提前返回")
        print("!")
        print("    【怎么用这份快照】")
        print("      先看 LENGTH 与写入值是否相符 —— 2026-09-21 的实例就是")
        print("      `写 614400 读回 8192`（= 写入 mod 16384），")
        print("      根因是 BD 里 AXI DMA 的 `c_sg_length_width` 用了默认的")
        print("      14 位（单次传输上限 16383 字节）。见 skill/pitfalls P10。")
        print("!")
        print("      其它常见组合：")
        print("        CTRL.ap_start=0 且 ap_idle=1  → IP 没被启动")
        print("        CTRL.ap_start=1 且 ap_idle=0  → IP 在跑（正常，等 done）")
        print("        DMASR 带 *Err                 → 总线地址问题")
        print("        dma_out 无 IOC 而 dma_in 有   → 上游没吐出数据")
        print("!" * 69 + "\n")

    # -----------------------------------------------------------------
    def get_result(self):
        """取 96x96 结果（numpy 数组）"""
        if self.out_buf is None:
            raise RuntimeError("先调 setup_dma()")
        _need_numpy("get_result()")
        return np.array(self.out_buf, dtype=np.uint8).reshape(OUT_SIZE, OUT_SIZE)

    def fill_test_pattern(self):
        """填一张测试图（无摄像头时用来验证数据通路）

        用"左暗右亮 + 居中亮块"的图案 —— 可预测，
        输出全黑或全白时一眼能看出是阈值问题。
        """
        if self.in_buf is None:
            raise RuntimeError("先调 setup_dma()")

        # RGB565: 中灰 = 0x8410
        _need_numpy("fill_test_pattern()")
        buf = np.frombuffer(self.in_buf, dtype=np.uint16)

        # numpy 视图：整块填中灰
        buf[:] = 0x8410

        # 居中 320x320 做水平渐变（保证 ROI 内有明暗变化）
        rx = (IN_WIDTH  - 320) // 2
        ry = (IN_HEIGHT - 320) // 2
        for y in range(ry, ry + 320, 4):        # 每 4 行取一行，够快
            row = y * (IN_WIDTH // 2)
            for x in range(rx, rx + 320, 4):
                g = (x - rx) * 255 // 320
                r5, g6, b5 = (g >> 3) & 0x1F, (g >> 2) & 0x3F, (g >> 3) & 0x1F
                buf[row + x // 2] = (r5 << 11) | (g6 << 5) | b5

        self.in_buf.flush()
        print("已填入测试图案（居中 320x320 渐变）")

    def show(self):
        """在 Jupyter 里出图"""
        img = self.get_result()
        import matplotlib.pyplot as plt
        plt.figure(figsize=(4, 4))
        plt.imshow(img, cmap='gray', vmin=0, vmax=255)
        # ⚠ 标题用**英文** —— 板上 matplotlib 没有中文字体。
        #   写中文的后果不是报错，而是刷一串
        #       UserWarning: Glyph 39044 (...) missing from current font
        #   并且标题渲染成豆腐块 —— 看起来像"图没出来"，其实是字体的锅。
        plt.title("96x96 preprocessing output")
        plt.axis('off')
        plt.show()
        return img

    def stats(self):
        """不进 Jupyter 也能看的统计"""
        img = self.get_result()
        nz = int((img > 0).sum())
        print("输出统计: min=%d max=%d mean=%.1f 非零=%d/%d" % (
            img.min(), img.max(), img.mean(), nz, OUT_PIXELS))
        return img


# =====================================================================
#  直接运行时：加载 + 自检
# =====================================================================
if __name__ == "__main__":
    print("=" * 69)
    print(" 手势识别流水线 —— PYNQ 自检")
    print("=" * 69)

    g = GesturePipeline()
    g.print_info()
    g.setup_dma()
    g.config()
    g.fill_test_pattern()
    g.run_once()
    g.stats()
    print("\n完成。若在 Jupyter 里，用 g.show() 看图。")
