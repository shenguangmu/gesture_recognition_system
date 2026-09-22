#!/usr/bin/env python3
"""
camera_probe.py —— 摄像头通路验证（VDMA 抓一帧）

【为什么用它】

项目没有示波器/逻辑分析仪，DVP 的 XCLK/PCLK/HREF/VSYNC 都看不到。
但摄像头这条路是**通的**：

    OV5640 --DVP--> dvp_capture --AXIS--> VDMA S2MM --> HP1 --> DDR

**VDMA 有完整的 AXI-Lite 接口**，所以可以反过来用它的状态寄存器
推断前端是否出图 —— 这是无仪器条件下唯一能做的端到端验证。

【能证明什么 / 不能证明什么】

  能：VDMA 收到帧 → 说明 XCLK 起了、SCCB 配成功了、DVP 有时序和数据
  不能：失败时**无法区分**是 XCLK 没起 / SCCB 没配好 / DVP 接线错 / 时序不对
        —— 那需要示波器。本脚本只能尽量把"卡在哪一类"缩窄。

【寄存器偏移来源】

从 `.hwh` 提取（`ip_contract.py`），**不是凭记忆**。
⚠ 注意 `S2MM_VSIZE` 在 `0xA0` —— `0x50` 是 **MM2S** 的同名寄存器，别搞混。

用法（板上）：
    sudo -E /usr/local/share/pynq-venv/bin/python3 camera_probe.py
    sudo -E /usr/local/share/pynq-venv/bin/python3 camera_probe.py --out cam.bin
"""

import argparse
import io
import sys
import time

# ⚠ 这层 UTF-8 包装**只在真实控制台需要**，而且必须加守卫。
#
#   背景：中文 Windows 控制台是 GBK，打印 ⚠ → 会 UnicodeEncodeError。
#   但 **Jupyter/IPython 的 sys.stdout 是 `OutStream`，没有 `.buffer`**，
#   直接包装会崩：
#       AttributeError: 'OutStream' object has no attribute 'buffer'
#   而 `%run camera_probe.py` 在 Jupyter 里跑是完全正常的用法 ——
#   所以必须用 getattr 守卫，而不是无条件包装。
#
#   （板子上是 Linux/UTF-8，本来也不需要这层；加它是为了脚本能在
#     PC 上做静态检查/试跑时不崩。）
if hasattr(getattr(sys.stdout, 'buffer', None), 'write') \
   and getattr(sys.stdout, 'encoding', '').lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ---- VDMA（xilinx.com:ip:axi_vdma:6.3），偏移取自 .hwh ----
V_S2MM_VDMACR      = 0x30
V_S2MM_VDMASR      = 0x34
V_PARK_PTR_REG     = 0x28
V_S2MM_VSIZE       = 0xA0
V_S2MM_HSIZE       = 0xA4
V_S2MM_FRMDLY_STRIDE = 0xA8
V_S2MM_SA = [0xAC, 0xB0, 0xB4]      # SA1..SA3（用 3 个 fstore）

# VDMACR 位
CR_RS        = 0x0001      # Run/Stop
CR_CIRCULAR  = 0x0002      # Circular_Park：1=环形，0=停在某帧
CR_RESET     = 0x0004      # 软复位

# VDMASR 位
SR_HALTED      = 0x0001
SR_VDMAIntErr  = 0x0010
SR_VDMASlvErr  = 0x0020
SR_VDMADecErr  = 0x0040
SR_SOFEarlyErr = 0x0080
SR_EOLEarlyErr = 0x0100
# 帧计数在 bit16..23（IRQFrameCntSts）

W, H = 640, 480
STRIDE = W * 2               # RGB565：1280 字节/行
FRAME_BYTES = STRIDE * H     # 614400
NFSTORE = 3


def sr_str(sr):
    """VDMASR 解码"""
    if sr == 0xFFFFFFFF:
        return '!! 读回全 1 —— 地址可能不在总线上 [0xFFFFFFFF]'
    bits = [
        (SR_HALTED, 'Halted'), (0x0002, 'Idle'),
        (SR_VDMAIntErr, 'VDMAIntErr'), (SR_VDMASlvErr, 'VDMASlvErr'),
        (SR_VDMADecErr, 'VDMADecErr'), (SR_SOFEarlyErr, 'SOFEarlyErr'),
        (SR_EOLEarlyErr, 'EOLEarlyErr'),
    ]
    hit = [n for m, n in bits if sr & m]
    frm = (sr >> 16) & 0xFF
    return ('%s | 帧计数=%d | [0x%08X]'
            % (', '.join(hit) if hit else '（无标志位）', frm, sr))


def main():
    ap = argparse.ArgumentParser(description='摄像头通路验证（VDMA 抓帧）')
    ap.add_argument('--out', metavar='PATH', help='收到帧后存成 .bin')
    ap.add_argument('--seconds', type=float, default=10.0, help='等待秒数')
    ap.add_argument('--bit', metavar='PATH', default='/home/xilinx/gesture_system.bit',
                    help='overlay 的 .bit 路径（⚠ 必须与当前修复版一致，'
                         '板上有多个版本时务必显式指定）')
    args = ap.parse_args()

    from pynq import Overlay, allocate
    import numpy as np

    print('=' * 70)
    print('  摄像头通路验证 —— VDMA 抓一帧')
    print('=' * 70)

    ol = Overlay(args.bit)
    print('    [overlay] %s' % args.bit)

    # ---------- 绕开 PYNQ 的 AxiVDMA 驱动 ----------
    #
    #  ⚠ PYNQ 3.x 的 `AxiVDMA.__init__` 要访问 `self.s2mm_introut` 找中断，
    #    但**本 BD 里所有中断都悬空**（没接 PS 的 IRQ_F2P —— 项目全程走轮询），
    #    于是 `ol.vdma` 直接抛 AttributeError，专用驱动根本用不了。
    #
    #  解法：**从清理后的 ip_dict 条目构造 DefaultIP**。
    #        pop 掉 'interrupts' 和 'driver' 两个字段 —— 它们是 PYNQ
    #        选择专用驱动的开关；去掉后 `DefaultIP` 只做纯 MMIO 访问。
    #
    #  2026-09-21 板上实测（见 host/vdma_bypass_test.py）：
    #    ✅ 方案 A（本方法）  → VERSION=0x62000050，可读可写
    #    ✅ 方案 B（裸 MMIO） → 同样可读
    #    ❌ 方案 C（原地改 ip_dict 再取 ol.vdma）→ 仍触发专用驱动，不可用
    #
    #  ⚠ 我们本来也不需要 PYNQ 的 VDMA 驱动 —— 只用它读写寄存器。
    from pynq import DefaultIP as _DefaultIP
    vdma_info = dict(ol.ip_dict['vdma'])
    vdma_info.pop('interrupts', None)
    vdma_info.pop('driver', None)
    v = _DefaultIP(vdma_info)
    print('    [绕行] VDMA 以 DefaultIP 访问（BD 中断悬空，'
          'PYNQ 的 AxiVDMA 驱动构造会失败）')

    # ---------- [1] 基本状态 ----------
    print('\n[1] VDMA 基本状态')
    ver = v.read(0x2C)
    print('    VDMA_VERSION = 0x%08X  (版本寄存器，能读说明 AXI-Lite 通)' % ver)
    print('    S2MM_VDMASR  = %s' % sr_str(v.read(V_S2MM_VDMASR)))

    # ---------- [2] 等 SCCB 配置完成 ----------
    #  sccb_master 是自由运行的：overlay 加载 → rst_n 释放 → 自动跑 250 条配置。
    #  100 kHz SCCB，每条 4 字节事务 ≈ 0.4 ms → 250 条约 100 ms。
    #  ⚠ sccb_0 **没有 AXI 接口**，cfg_done 软件读不到，只能等固定时间。
    print('\n[2] 等 SCCB 配置完成（自由运行，约 100 ms；软件读不到状态）')
    print('    ⚠ sccb_0 是纯 RTL 模块，cfg_done/cfg_error 没接 AXI —— 无法查询')
    time.sleep(0.5)
    print('    已等 0.5 s')

    # ---------- [3] 分配 3 个帧缓冲 ----------
    print('\n[3] 分配 %d 个帧缓冲（每个 %d 字节）' % (NFSTORE, FRAME_BYTES))
    bufs = [allocate(shape=(FRAME_BYTES,), dtype=np.uint8) for _ in range(NFSTORE)]
    for i, b in enumerate(bufs):
        b[:] = 0
        b.flush()
        print('    buf%d @ 0x%08X' % (i, b.physical_address))

    # ---------- [4] 配置 S2MM ----------
    print('\n[4] 配置 S2MM（环形模式，%dx%d RGB565）' % (W, H))
    v.write(V_S2MM_VDMACR, CR_RESET)
    t0 = time.time()
    while not (v.read(V_S2MM_VDMASR) & SR_HALTED):
        if time.time() - t0 > 1.0:
            print('    [FAIL] 软复位超时：%s' % sr_str(v.read(V_S2MM_VDMASR)))
            return 1
    print('    软复位完成（Halted=1）')

    v.write(V_S2MM_VSIZE, H)
    v.write(V_S2MM_HSIZE, STRIDE)
    v.write(V_S2MM_FRMDLY_STRIDE, STRIDE)
    for i, off in enumerate(V_S2MM_SA):
        v.write(off, bufs[i].physical_address)

    print('    VSIZE=%d  HSIZE=%d  STRIDE=%d' % (H, STRIDE, STRIDE))
    print('    回读 VSIZE=%d HSIZE=%d STRIDE=%d'
          % (v.read(V_S2MM_VSIZE), v.read(V_S2MM_HSIZE),
             v.read(V_S2MM_FRMDLY_STRIDE)))

    # Park 指针：S2MM 停在第 0 帧（bit1 = S2MM_park 使能）
    v.write(V_PARK_PTR_REG, 0x2)

    # ---------- [5] 启动 ----------
    print('\n[5] 启动 S2MM')
    # ⚠ 读-改-写，不要整字覆盖：复位值 0x10042 里还带着
    #   Circular_Park(bit1) 和 IRQFrameCount(bit16..23)，直接写整字会清掉它们。
    cr = v.read(V_S2MM_VDMACR)
    print('    启动前 S2MM_VDMACR = 0x%08X' % cr)
    new_cr = cr | CR_RS | CR_CIRCULAR
    v.write(V_S2MM_VDMACR, new_cr)
    back = v.read(V_S2MM_VDMACR)
    print('    写 0x%08X → 读回 0x%08X  RS=%d Circular=%d %s'
          % (new_cr, back, back & 1, (back >> 1) & 1,
             'OK' if (back & CR_RS) else '⚠ RS 没锁存'))

    # ---------- [6] 轮询 ----------
    print('\n[6] 轮询 %.0f 秒' % args.seconds)
    print('    ⚠ 判据是**帧计数在本次运行中是否增长**，不是"是否非零" ——')
    print('      VDMASR 的帧计数位可能带着复位前的陈旧值（2026-09-21 踩过：')
    print('      启动后读到 1 就误报"收到帧"，其实 10 秒里一次都没动）。')
    print()
    frm0 = (v.read(V_S2MM_VDMASR) >> 16) & 0xFF
    print('    基线帧计数 = %d（以此为起点，只有变大才算真收到）' % frm0)
    print('    %-7s %s' % ('t', 'S2MM_VDMASR'))

    t0 = time.time()
    last_frm = frm0
    arrived = 0
    while time.time() - t0 < args.seconds:
        sr = v.read(V_S2MM_VDMASR)
        frm = (sr >> 16) & 0xFF
        # ⚠ 帧计数是 8 位，会回绕（0xFF→0），用 != 而不是 >
        if frm != last_frm:
            arrived += 1
            mark = '  ← 帧计数变化：%d → %d' % (last_frm, frm)
            last_frm = frm
        else:
            mark = ''
        print('    %-7.1f %s%s' % (time.time() - t0, sr_str(sr), mark))
        time.sleep(0.5)

    # ---------- [7] 结果 ----------
    print('\n[7] 结果')
    sr = v.read(V_S2MM_VDMASR)
    frm_end = (sr >> 16) & 0xFF
    print('    最终 S2MM_VDMASR = %s' % sr_str(sr))
    print('    帧计数：基线 %d → 结束 %d，本次运行**变化 %d 次**'
          % (frm0, frm_end, arrived))

    park = v.read(V_PARK_PTR_REG) & 0x1F
    idx = min(park, NFSTORE - 1)
    buf = bufs[idx]
    buf.invalidate()
    arr = np.array(buf)
    nz = int(np.count_nonzero(arr))
    print('    停在 buf%d，非零字节 %d/%d (%.1f%%)'
          % (idx, nz, arr.size, 100.0 * nz / arr.size))
    print('    前 16 字节 = %s' % arr[:16].tolist())

    print()
    if arrived > 0 and nz > 0:
        print('    ✅ **摄像头通路是通的** —— 帧在增长，且缓冲区有真实像素')
        if args.out:
            buf.tofile(args.out)
            print('    已存 %s' % args.out)
    elif arrived > 0 and nz == 0:
        print('    ⚠ **帧计数在涨，但缓冲区全零** —— 这是典型的')
        print('       「控制信号到了、数据线没到」的组合：')
        print('         HREF/VSYNC/PCLK 在动 → VDMA 正常地"写完"一帧')
        print('         但 D0–D7 恒 0      → 写进去的全是零')
        print('       先查 **Pmod B 的 8 根数据线**（D0–D7，模块上是交错排的）')
    elif arrived == 0 and nz == 0:
        print('    ❌ **10 秒内帧计数一次都没变，缓冲区全零**')
        print('       说明 VDMA 一帧都没收到 —— 前端（dvp_capture）没出数据')
        print()
        print('       可能原因（**无仪器时无法区分**，按可能性排）：')
        print('         1. XCLK 没输出        → PL 侧 Clocking Wizard')
        print('         2. SCCB 没配成功      → 寄存器表 / SDA-SCL 接线 / 上拉')
        print('         3. DVP 接线错         → 引脚映射（**镜像编号！**）')
        print('         4. 摄像头没供电/坏')
        print()
        print('       ⚠ 区分这四条**需要示波器/逻辑分析仪**（手册 §5.3）：')
        print('         io_xclk → io_scl → io_pclk → HREF/VSYNC → 数据')
        print('         · io_xclk 没波形   → PL 问题，别动摄像头')
        print('         · XCLK 有、PCLK 无 → SCCB 没配好')
        print('         · PCLK 有、无帧    → 时序或数据线')
    else:
        print('    ⚠ 组合异常：帧计数变化 %d 次，非零字节 %d' % (arrived, nz))

    return 0


if __name__ == '__main__':
    sys.exit(main())
