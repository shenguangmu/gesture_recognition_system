#!/usr/bin/env python3
"""
ap_done_probe.py —— 最小化验证：IP 到底跑没跑、DMA 到底搬没搬

【为什么要有这个脚本】

dma_diag 的 v1/v2 都在「怎么解读 DMA 状态寄存器」上出错：
  · v1 把 DMASR 的 bit1(IDLE) 当成 IOC
  · v2 复位时写了 DMACR=0，导致后续读到的值可能是 DMACR 而不是 DMASR
两次的 DMA 结论都不可信。

本脚本**不再依赖对 DMA 状态位的解读**，只测两个最硬的事实：

  ① **ap_done 是什么时候置位的** —— 用软件计时，不靠轮询读 CTRL
     （ap_done 是 COR，一读就掉；所以只读一次，读完把结果记下来）
  ② **输出 buffer 有没有被写进东西** —— 直接扫内存
     DMA 真搬了数据，out_buf 就不该全是 0

用法（板上）：
    sudo -E /usr/local/share/pynq-venv/bin/python3 ap_done_probe.py
"""

import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BIT = sys.argv[1] if len(sys.argv) > 1 else '/home/xilinx/gesture_system.bit'

P_CTRL, P_ISR = 0x00, 0x0C
MM2S_DMACR, MM2S_DMASR, MM2S_SRCADDR, MM2S_LENGTH = 0x00, 0x04, 0x18, 0x28
S2MM_DMACR, S2MM_DMASR, S2MM_DSTADDR, S2MM_LENGTH = 0x30, 0x34, 0x48, 0x58


def main():
    from pynq import Overlay, allocate
    import numpy as np

    ol = Overlay(BIT)
    p, din, dout = ol.gesture_preproc_0, ol.dma_in, ol.dma_out

    print('=' * 70)
    print('  ap_done_probe —— 只测「IP 跑没跑」和「DMA 搬没搬」')
    print('=' * 70)

    # ---------------- ① 寄存器回环测试 ----------------
    print('\n[1] 寄存器回环：写一个特征值，看能不能读回来')
    print('    （DMACR 是普通读/写寄存器，最适合做回环）')
    for nm, ip, cr, sr in [('dma_in', din, MM2S_DMACR, MM2S_DMASR),
                           ('dma_out', dout, S2MM_DMACR, S2MM_DMASR)]:
        save = ip.read(cr)
        ip.write(cr, 0x1001)                 # RUNSTOP | IOC_IRQEn
        back_cr = ip.read(cr)
        back_sr = ip.read(sr)
        print('    %-8s DMACR 写 0x1001 → 读回 0x%08X %s' % (
            nm, back_cr, 'OK' if back_cr == 0x1001 else '!! 不匹配'))
        print('    %-8s DMASR = 0x%08X  (HALTED=%d IDLE=%d)' % (
            nm, back_sr, back_sr & 1, (back_sr >> 1) & 1))
        ip.write(cr, save)

    # ---------------- ② 分配 + 填输入 ----------------
    print('\n[2] 分配缓冲，输入填非零图案')
    in_buf = allocate(shape=(640 * 480,), dtype=np.uint16)
    out_buf = allocate(shape=(96 * 96,), dtype=np.uint8)
    in_buf[:] = 0x1234                       # 明显非零
    out_buf[:] = 0                           # 输出先清零，便于判定
    in_buf.flush()
    out_buf.flush()
    print('    in  @ 0x%08X   前 8 字节 = %s' % (
        in_buf.physical_address, np.array(in_buf[:8]).tolist()))
    print('    out @ 0x%08X   前 8 字节 = %s' % (
        out_buf.physical_address, np.array(out_buf[:8]).tolist()))

    # ---------------- ③ 启动 ----------------
    print('\n[3] 启动（S2MM → MM2S → ap_start）')
    dout.write(S2MM_DSTADDR, out_buf.physical_address)
    dout.write(S2MM_DMACR, 0x1001)
    dout.write(S2MM_LENGTH, out_buf.nbytes)

    din.write(MM2S_SRCADDR, in_buf.physical_address)
    din.write(MM2S_DMACR, 0x1001)
    din.write(MM2S_LENGTH, in_buf.nbytes)

    before = p.read(P_CTRL)
    print('    ap_start 前 CTRL = 0x%08X' % before)

    t0 = time.time()
    p.write(P_CTRL, before | 0x01)

    # ---------------- ④ 只读一次 CTRL，看多久置 done ----------------
    print('\n[4] ap_start 后立刻连读 CTRL（每次读都会清 ap_done，')
    print('    所以第一次读到 done=1 的时间就是完成时间）')
    done_at = None
    samples = []
    while time.time() - t0 < 3.0:
        c = p.read(P_CTRL)
        samples.append((time.time() - t0, c))
        if c & 0x02:                          # ap_done
            done_at = time.time() - t0
            break
    for dt, c in samples[:10]:
        print('    t=%.4fs  CTRL=0x%08X  done=%d' % (dt, c, (c >> 1) & 1))
    if len(samples) > 10:
        print('    ...（共 %d 次采样）' % len(samples))

    print('\n    P_ISR = 0x%08X  (bit0 是 ap_done 的粘滞标志)' % p.read(P_ISR))

    # ---------------- ⑤ 最硬的一条：输出 buffer 变了吗 ----------------
    time.sleep(0.3)
    out_buf.invalidate()
    arr = np.array(out_buf)
    nz = int(np.count_nonzero(arr))
    print('\n[5] 输出 buffer 内容（DMA 有没有真的写进去）')
    print('    非零字节数 = %d / %d' % (nz, arr.size))
    print('    前 16 字节 = %s' % arr[:16].tolist())

    # ---------------- ⑥ 结论 ----------------
    print('\n[6] 结论')
    if done_at is not None and done_at < 0.01:
        print('    ⚠ ap_done 在 %.4f s 就置位 —— **IP 瞬间"完成"，没干活**' % done_at)
    elif done_at is not None:
        print('    ap_done 在 %.4f s 置位' % done_at)

    if nz == 0:
        print('    ❌ 输出全 0 —— **DMA 没有写进任何数据**')
        print('       结合上面的 ap_done，最可能是：IP 的 AXI-Stream 没接到数据，')
        print('       或者 IP 根本没被真正启动（BD 侧接线/复位问题）。')
    else:
        print('    ✅ 输出有 %d 个非零字节 —— **DMA 确实搬了数据**' % nz)
        print('       → 数据通路是通的，问题在 bringup_check.py 的 ap_done 判定')


if __name__ == '__main__':
    sys.exit(main())
