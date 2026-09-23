#!/usr/bin/env python3
"""
ap_start_probe.py —— 验证 `ap_start` 到底有没有真正送达 IP

【为什么要有这个脚本】

2026-09-21 上板实测的核心矛盾：

    现象 A：ap_start 后 0.0001 秒 ap_done 就置位
    现象 B：输出 buffer 9216 字节全 0，DMA 无 IOC

但 `gesture_preproc` 的 HLS 源码里**没有任何"快速完成"的路径**：
参数检查（width/height/roi）全部合法，不会提前 return；
主循环要读 AXI-Stream，而 AXI-Stream 是**阻塞**的 ——
**IP 若在等数据，ap_done 只会等到超时，不可能 0.1 ms 就置位。**

唯一的解释：**IP 压根没进入流水线**，即 `ap_start` 没真正生效。

本脚本不再猜"IP 做了什么"，而是直接验证**控制通路是否有效**：

  [1] ap_start 连写两次 —— II=1 的 IP 从 ap_start 到 ap_idle 拉低
      应有几十 ns 窗口，连写几乎必然踩中
  [2] 扫描全部 16 个寄存器偏移 —— 看有没有非预期的可写位置
  [3] 对比 IER 回环 —— IER 是普通读/写寄存器，不像 CTRL 有 COR 语义
  [4] 空跑对照 —— 只 ap_start、不启 DMA，看是否同样"瞬间完成"

用法（板上）：
    sudo -E /usr/local/share/pynq-venv/bin/python3 ap_start_probe.py
"""

import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BIT = sys.argv[1] if len(sys.argv) > 1 else '/home/xilinx/gesture_system.bit'

P_CTRL, P_GIE, P_IER, P_ISR = 0x00, 0x04, 0x08, 0x0C

# HLS 生成的 16 个寄存器（见 xgesture_preproc_hw.h）
REGNAMES = {
    0x00: 'AP_CTRL', 0x04: 'GIE', 0x08: 'IER', 0x0C: 'ISR',
    0x10: 'width', 0x18: 'height', 0x20: 'thresh_mode', 0x28: 'thresh_offset',
    0x30: 'gauss_en', 0x38: 'sobel_en', 0x40: 'morph_en', 0x48: 'gain',
    0x50: 'roi_x', 0x58: 'roi_y', 0x60: 'roi_w', 0x68: 'roi_h',
}


def ctrl(c):
    return ('CTRL=0x%08X [ap_start=%d ap_done=%d ap_idle=%d ap_ready=%d]'
            % (c, c & 1, (c >> 1) & 1, (c >> 2) & 1, (c >> 3) & 1))


def main():
    from pynq import Overlay
    ol = Overlay(BIT)
    p = ol.gesture_preproc_0

    print('=' * 70)
    print('  ap_start_probe —— ap_start 到底送达没有')
    print('=' * 70)

    # ---------------- [1] ap_start 连写两次 ----------------
    print('\n[1] ap_start 连写两次')
    print('    II=1 的 IP 从 ap_start 到 ap_idle 拉低有几十 ns 窗口；')
    print('    连写两次几乎必然踩中。读到的第二次结果 = 第一次是否真的启动了。')
    print()

    p.write(P_CTRL, 0)                      # 先停干净
    time.sleep(0.05)
    print('    空闲态       %s' % ctrl(p.read(P_CTRL)))

    for trial in range(3):
        p.write(P_CTRL, 0)
        time.sleep(0.02)
        # 连写两次 ap_start，中间不读
        p.write(P_CTRL, 1)
        p.write(P_CTRL, 1)
        after = p.read(P_CTRL)              # 这一次读会清掉 ap_done
        print('    试 %d: 连写两次后  %s' % (trial + 1, ctrl(after)))
        if (after >> 2) & 1 == 0:
            print('         ↑ ap_idle=0 —— **IP 真的启动了**（正在跑）')
        else:
            print('         ↑ ap_idle=1 —— 第二次仍读到"空闲"，'
                  '说明第一次 ap_start 没让它动')

    # ---------------- [2] 扫描全部 16 个寄存器 ----------------
    print('\n[2] 扫描 16 个寄存器偏移（写特征值 → 读回）')
    print('    用于找"哪些偏移是真的可写寄存器"')
    print('    %-6s %-14s %-12s %-12s %s' % ('偏移', '名称', '写前', '写后', '判定'))
    for off in sorted(REGNAMES):
        before = p.read(off)
        p.write(off, 0x5A5A5A5A)            # 好认的特征值
        after = p.read(off)
        p.write(off, before)                # 还原
        verdict = '可写' if after == 0x5A5A5A5A else (
            '部分/只读' if after != before else '无变化')
        print('    0x%02X   %-14s 0x%08X  0x%08X   %s'
              % (off, REGNAMES[off], before, after, verdict))

    # ---------------- [3] IER 回环 ----------------
    print('\n[3] IER 回环（IER 是普通读/写寄存器，不像 CTRL 有 COR 语义）')
    save = p.read(P_IER)
    ok_ier = True
    for v in (0x1, 0x2, 0x3, 0x0):
        p.write(P_IER, v)
        back = p.read(P_IER)
        hit = '✅' if back == v else '❌'
        if back != v:
            ok_ier = False
        print('    写 0x%08X → 读 0x%08X  %s' % (v, back, hit))
    p.write(P_IER, save)
    print('    %s' % ('IER 回环正常' if ok_ier else
                      'IER 回环**不正常** —— AXI-Lite 写通路可能有问题'))

    # ---------------- [4] 空跑对照 ----------------
    print('\n[4] 空跑：只 ap_start，不启任何 DMA')
    print('    若同样"瞬间 done"，说明完成与数据无关 → 控制通路问题')
    for trial in range(3):
        p.write(P_CTRL, 0)
        time.sleep(0.02)
        t0 = time.time()
        p.write(P_CTRL, 1)
        c = p.read(P_CTRL)
        dt = time.time() - t0
        print('    试 %d: 耗时 %.6f s  %s' % (trial + 1, dt, ctrl(c)))

    # 再读一次，看 ap_done 是不是立刻又可读
    p.write(P_CTRL, 0)
    time.sleep(0.02)
    p.write(P_CTRL, 1)
    c1 = p.read(P_CTRL)
    c2 = p.read(P_CTRL)
    print('    连读两次: 第一次 %s' % ctrl(c1))
    print('              第二次 %s' % ctrl(c2))
    print('    （ap_done 是 COR，读完即清：第一次 done=1、第二次 done=0 属正常）')

    # ---------------- 结论 ----------------
    print('\n' + '=' * 70)
    print('  怎么读这份结果')
    print('=' * 70)
    print('''
  [1] 若连写两次后 ap_idle 仍恒为 1
        → **ap_start 没让 IP 动**。IP 的 ap_ctrl 通路有问题，
          或这个 IP 根本不是 ap_ctrl_hs（例如综合成了 ap_ctrl_none）。
          → 下一步查 HLS 综合报告里的 `ap_ctrl` 类型。

  [2] 若只有 CTRL/GIE/IER/ISR 可写、其余 12 个参数寄存器读回=写前
        → 参数寄存器是"只写"或读回被屏蔽，属正常。
          但若**连参数寄存器都写不进**，AXI-Lite 通路就有问题。

  [3] IER 回环失败 → AXI-Lite **写**通路有问题（比 IP 逻辑问题更底层）。

  [4] 空跑也"瞬间 done" → 完成与数据无关，坐实控制通路问题。
''')


if __name__ == '__main__':
    sys.exit(main())
