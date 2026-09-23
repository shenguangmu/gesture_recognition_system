#!/usr/bin/env python3
"""
dma_diag.py —— 查 dma_in / dma_out 卡在哪（在 PYNQ 板上跑）

bringup_check.py 的 [3d] 报「等 ap_done 超时」时，用它看**数据到底有没有流**。

用法（板上）：
    sudo -E /usr/local/share/pynq-venv/bin/python3 dma_diag.py
    sudo -E /usr/local/share/pynq-venv/bin/python3 dma_diag.py /home/xilinx/gesture_system.bit

⚠ 只读寄存器 + 跑一帧，不写任何文件。

【v2 相对 v1 的修正】

  1. **DMASR 解读用错了位**：AXI DMA 的 DMASR 里 **bit1 = IDLE**（1 = 通道空闲），
     不是「传输完成」。v1 把 bit1 当成 IOC 来读，导致「IDLE=0」被误读成「在忙」。
     IOC 是 **bit12**。

  2. **ap_done 是 COR（读时清除）**：CTRL 的 bit1 一读就掉。
     v1 在轮询循环里边读边判，读到的 0 不能证明「没跑完」。
     v2 改成：轮询时**不读 CTRL**，只读 DMASR；循环结束后再取一次 CTRL 快照。
     —— 这样 CTRL 的快照才可信。

  3. 增加了 **IER / ISR 读取**：ISR 的 bit0 是 ap_done 的**粘滞标志**
     （Read/TOW，写 1 清除），不受「读 CTRL 清 done」影响，
     是判断「跑完过没有」最可靠的依据。
"""

import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BIT = sys.argv[1] if len(sys.argv) > 1 else '/home/xilinx/gesture_system.bit'

# ---- preproc 的 AXI-Lite 寄存器（偏移已与 xgesture_preproc_hw.h 核对一致）----
P_CTRL, P_GIE, P_IER, P_ISR = 0x00, 0x04, 0x08, 0x0C

# ---- AXI DMA 寄存器（Xilinx PG021）----
MM2S_DMACR, MM2S_DMASR, MM2S_SRCADDR, MM2S_LENGTH = 0x00, 0x04, 0x18, 0x28
S2MM_DMACR, S2MM_DMASR, S2MM_DSTADDR, S2MM_LENGTH = 0x30, 0x34, 0x48, 0x58

DMACR_RS = 0x0001          # bit0  RUN/STOP
DMACR_RESET = 0x0004       # bit2  RESET
DMACR_IOC_IRQEN = 0x1000   # bit12 完成中断使能

# DMASR 位（PG021）
SR = [
    (0x0001, 'HALTED'),
    (0x0002, 'IDLE'),
    (0x0010, 'DMAIntErr'),
    (0x0020, 'DMASlvErr'),
    (0x0040, 'DMADecErr'),
    (0x1000, 'IOC_Irq'),
    (0x2000, 'Dly_Irq'),
    (0x4000, 'Err_Irq'),
]


def decode(sr):
    """把 DMASR 拆成人能读的位名"""
    if sr == 0xFFFFFFFF:
        return '!! 总线读回全 1 —— 地址可能不在总线上'
    hits = [n for m, n in SR if sr & m]
    return (', '.join(hits) if hits else '（无标志位）') + '  [raw=0x%08X]' % sr


def ctrl_str(c):
    return ('CTRL=0x%08X  [ap_start=%d ap_done=%d ap_idle=%d ap_ready=%d]'
            % (c, c & 1, (c >> 1) & 1, (c >> 2) & 1, (c >> 3) & 1))


def main():
    from pynq import Overlay, allocate
    import numpy as np                       # noqa: F401  allocate() 需要它

    print('=' * 70)
    print('  dma_diag v2 —— 定位 ap_done 超时')
    print('=' * 70)

    ol = Overlay(BIT)
    p = ol.gesture_preproc_0
    din, dout = ol.dma_in, ol.dma_out

    print('\n[1] IP 类型')
    print('    preproc = %s' % type(p).__name__)
    print('    dma_in  = %s' % type(din).__name__)
    print('    dma_out = %s' % type(dout).__name__)

    print('\n[2] 复位前的 DMA 状态')
    print('    MM2S_DMASR = %s' % decode(din.read(MM2S_DMASR)))
    print('    S2MM_DMASR = %s' % decode(dout.read(S2MM_DMASR)))

    print('\n[3] 分配缓冲')
    in_buf = allocate(shape=(640 * 480,), dtype=np.uint16)
    out_buf = allocate(shape=(96 * 96,), dtype=np.uint8)
    in_buf[:] = 0x1234
    in_buf.flush()
    print('    in  @ 0x%08X  (%d B)' % (in_buf.physical_address, in_buf.nbytes))
    print('    out @ 0x%08X  (%d B)' % (out_buf.physical_address, out_buf.nbytes))

    print('\n[4] 软复位两个 DMA')
    for nm, ip, sr_o, cr_o in [('dma_in', din, MM2S_DMASR, MM2S_DMACR),
                               ('dma_out', dout, S2MM_DMASR, S2MM_DMACR)]:
        ip.write(cr_o, DMACR_RESET)
        t0 = time.time()
        while not (ip.read(sr_o) & 0x01):        # SR.HALTED = bit0
            if time.time() - t0 > 1.0:
                print('    [FAIL] %s 复位超时：%s' % (nm, decode(ip.read(sr_o))))
                break
        else:
            print('    [ OK ] %s 复位完成：%s' % (nm, decode(ip.read(sr_o))))
            # 复位后清 HALTED：写 RUNSTOP 让它回到可接收状态
            ip.write(cr_o, 0)

    print('\n[5] 按正确顺序启动：S2MM → MM2S → ap_start')
    dout.write(S2MM_DSTADDR, out_buf.physical_address)
    dout.write(S2MM_DMACR, DMACR_RS | DMACR_IOC_IRQEN)
    dout.write(S2MM_LENGTH, out_buf.nbytes)
    print('    武装 S2MM 后：%s' % decode(dout.read(S2MM_DMASR)))

    din.write(MM2S_SRCADDR, in_buf.physical_address)
    din.write(MM2S_DMACR, DMACR_RS | DMACR_IOC_IRQEN)
    din.write(MM2S_LENGTH, in_buf.nbytes)
    print('    启动 MM2S 后：%s' % decode(din.read(MM2S_DMASR)))

    print('    ap_start 前  %s' % ctrl_str(p.read(P_CTRL)))
    p.write(P_CTRL, p.read(P_CTRL) | 0x01)
    print('    ap_start 后  %s' % ctrl_str(p.read(P_CTRL)))

    print('\n[6] 轮询 5 秒')
    print('    ⚠ 只读 DMASR（不读 CTRL）—— CTRL 的 ap_done 是 COR，一读就掉')
    t0 = time.time()
    while time.time() - t0 < 5.0:
        mm, ss = din.read(MM2S_DMASR), dout.read(S2MM_DMASR)
        # bit1 = IDLE：1 表示空闲（搬完了或还没开始），0 表示正在搬
        print('    t=%.1fs  MM2S: IDLE=%d HALTED=%d ERR=%d IOC=%d | '
              'S2MM: IDLE=%d HALTED=%d ERR=%d IOC=%d' % (
                  time.time() - t0,
                  (mm >> 1) & 1, mm & 1, (mm >> 6) & 1, (mm >> 12) & 1,
                  (ss >> 1) & 1, ss & 1, (ss >> 6) & 1, (ss >> 12) & 1))
        # 两边都完成就走
        if (mm >> 12) & 1 and (ss >> 12) & 1:
            print('    → 两个通道都 IOC 置位，传输完成')
            break
        time.sleep(0.5)

    print('\n[7] 循环结束后的稳定快照（此时读 CTRL 是安全的）')
    c = p.read(P_CTRL)
    print('    %s' % ctrl_str(c))
    print('    P_ISR = 0x%08X   ← bit0=ap_done 粘滞标志, bit1=ap_ready' % p.read(P_ISR))
    print('    P_IER = 0x%08X' % p.read(P_IER))
    print('    MM2S_DMASR = %s' % decode(din.read(MM2S_DMASR)))
    print('    S2MM_DMASR = %s' % decode(dout.read(S2MM_DMASR)))

    print('\n[8] 结论')
    mm, ss = din.read(MM2S_DMASR), dout.read(S2MM_DMASR)
    isr = p.read(P_ISR)

    if (mm >> 12) & 1 and (ss >> 12) & 1:
        print('    ✅ 两个 DMA 都置了 IOC —— **数据搬完了**')
        print('    → 说明数据通路是通的！超时问题在 ap_done 的判定方式，')
        print('      不在硬件。bringup_check.py 的轮询逻辑要改。')
    elif mm & 1 or ss & 1:
        print('    ❌ 至少一个 DMA 仍 HALTED —— 通道没真正启动')
    elif (mm >> 6) & 1 or (ss >> 6) & 1:
        print('    ❌ DMA 报错（Err 位置位）—— 查 buffer 物理地址 / 总线映射')
    elif not ((mm >> 12) & 1) and not ((ss >> 12) & 1):
        print('    ❌ 两个 DMA 都没有 IOC —— 一次完整传输都没发生')
        print('       而 IDLE 若为 1 = 通道空转、没数据可搬 → 上游没供数')
    else:
        print('    ⚠ 只完成了一个通道 —— 另一个卡住')


if __name__ == '__main__':
    sys.exit(main())
