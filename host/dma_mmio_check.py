#!/usr/bin/env python3
"""
dma_mmio_check.py —— 确认 din/dout 的 MMIO 到底指向哪块地址

【为什么查这个】

len_loopback 的结果：
    dma_in  与 dma_out 写入同样的长度，**读回完全相同的值**
    （614400→8192、123456→8768、65536→0、8192→8192）

两个**独立的** AXI DMA 实例不可能读出一模一样的寄存器值 ——
除非我们读的**根本不是各自的寄存器**。

最可能的原因：Pynq 的 `DMA` 类**自己包了一层 mmio**，
`din.read(off)` 未必等于 `dma_in 的基地址 + off`。

本脚本直接对比：
  · overlay.ip_dict 里两个 DMA 的 phys_addr
  · 两个 DMA 对象各自的 .mmio.base_addr
  · 用裸 MMIO（按 ip_dict 的 phys_addr）读同一个偏移
  · 用 DMA 对象的 .read() 读同一个偏移
  —— 两者不一致的话，就找到根因了。

用法（板上）：
    sudo -E /usr/local/share/pynq-venv/bin/python3 dma_mmio_check.py
"""

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BIT = sys.argv[1] if len(sys.argv) > 1 else '/home/xilinx/gesture_system.bit'


def main():
    from pynq import Overlay, MMIO
    ol = Overlay(BIT)

    print('=' * 72)
    print('  dma_mmio_check —— din/dout 的 MMIO 指向哪')
    print('=' * 72)

    # ---------- [1] ip_dict 里的地址 ----------
    print('\n[1] overlay.ip_dict 里的 phys_addr')
    for n in ('dma_in', 'dma_out'):
        info = ol.ip_dict.get(n, {})
        print('    %-9s phys_addr = 0x%08X  addr_range = %s'
              % (n, info.get('phys_addr', 0), info.get('addr_range', '?')))

    # ---------- [2] DMA 对象的 mmio ----------
    print('\n[2] DMA 对象的 .mmio.base_addr')
    din, dout = ol.dma_in, ol.dma_out
    for n, ip in (('dma_in', din), ('dma_out', dout)):
        try:
            print('    %-9s .mmio.base_addr = 0x%08X   type=%s'
                  % (n, ip.mmio.base_addr, type(ip).__name__))
        except Exception as e:
            print('    %-9s 取 .mmio 失败: %s' % (n, e))
        for attr in ('mmio', 'mmio_mm2s', 'mmio_s2mm'):
            if hasattr(ip, attr):
                try:
                    a = getattr(ip, attr)
                    print('        .%s.base_addr = 0x%08X'
                          % (attr, a.base_addr))
                except Exception as e:
                    print('        .%s 取地址失败: %s' % (attr, e))

    # ---------- [3] 裸 MMIO vs DMA.read 对比 ----------
    print('\n[3] 同一个偏移，两种读法对比（写 0x2000 后读回）')
    print('    裸 MMIO = 直接按 ip_dict 的 phys_addr 建 MMIO')
    print('    DMA.read = 用 Pynq DMA 对象的 .read()')
    print()
    print('    %-9s %-6s %-12s %-12s %s'
          % ('DMA', '偏移', '裸MMIO读', 'DMA.read', '一致?'))
    for n, ip in (('dma_in', din), ('dma_out', dout)):
        base = ol.ip_dict[n]['phys_addr']
        raw = MMIO(base, 0x10000)
        for off, lbl in [(0x00, 'DMACR'), (0x28, 'MM2S_LEN'), (0x58, 'S2MM_LEN')]:
            raw.write(off, 0x2000)
            a = raw.read(off)
            b = ip.read(off)
            print('    %-9s 0x%02X %-6s 0x%08X   0x%08X   %s'
                  % (n, off, lbl, a, b, '✅' if a == b else '❌ 不一致'))

    # ---------- [4] 交叉验证：写 dma_in，看 dma_out 变不变 ----------
    print('\n[4] 交叉验证：只写 dma_in 的 LENGTH(0x28)，观察 dma_out')
    base_in = ol.ip_dict['dma_in']['phys_addr']
    base_out = ol.ip_dict['dma_out']['phys_addr']
    raw_in, raw_out = MMIO(base_in, 0x10000), MMIO(base_out, 0x10000)
    raw_in.write(0x28, 0x00010000)          # 65536
    print('    写 dma_in[0x28]=65536')
    print('      dma_in .read(0x28)  = %d' % din.read(0x28))
    print('      dma_out.read(0x28)  = %d' % dout.read(0x28))
    print('      裸 dma_in [0x28]    = %d' % raw_in.read(0x28))
    print('      裸 dma_out[0x28]    = %d' % raw_out.read(0x28))
    print('    ⚠ 若 dma_out 的读值跟着变 → 两者指向了同一块地址')

    print('\n' + '=' * 72)
    print('  判读')
    print('=' * 72)
    print('''
  [2] 若 din.mmio.base_addr 与 ip_dict 的 phys_addr**不同**
        → Pynq 的 DMA 对象根本不是按这个基地址访问的，
          我们的 read/write 一直打在了别的地方。

  [3] 裸 MMIO 与 DMA.read 不一致 → 同上。

  [4] 写 dma_in 却让 dma_out 变化 → 两个对象共用同一块地址，
      **所有针对 din/dout 的读写都不可信**。
''')


if __name__ == '__main__':
    sys.exit(main())
