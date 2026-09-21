#!/usr/bin/env python3
"""
reg_ident.py —— 逐个寄存器探针：弄清 AXI DMA 的地址到底怎么映射

【为什么需要它】

ap_done_probe 的 [1] 出现了一个无法解释的结果：
    MM2S_DMACR 写 0x1001 → 读回 0x00011003
标准和 AXI DMA（PG021）里 DMACR 只应丢 bit1，读回 0x1001。
现在多出 bit0/bit1/bit16，说明**我访问的地址不是我以为的那个寄存器**。

本脚本不猜，用**逐位单写单读**做对照实验：
  · 对每个候选偏移，分别只写 bit0 / bit1 / bit2 / bit12
  · 读回全部候选偏移，看哪个跟着变
  —— 变了的那个，才是这个地址真正对应的寄存器。

用法（板上）：
    sudo -E /usr/local/share/pynq-venv/bin/python3 reg_ident.py
"""

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BIT = sys.argv[1] if len(sys.argv) > 1 else '/home/xilinx/gesture_system.bit'

# 标准 AXI DMA 偏移（PG021，Direct Register 模式）
CAND = [
    ('MM2S_DMACR', 0x00), ('MM2S_DMASR', 0x04), ('MM2S_SA', 0x18),
    ('MM2S_LENGTH', 0x28), ('S2MM_DMACR', 0x30), ('S2MM_DMASR', 0x34),
    ('S2MM_DA', 0x48), ('S2MM_LENGTH', 0x58),
]


def snap(ip):
    """把全部候选偏移读一遍，返回 {偏移: 值}"""
    return {off: ip.read(off) for _, off in CAND}


def main():
    from pynq import Overlay

    ol = Overlay(BIT)
    print('=' * 70)
    print('  reg_ident —— AXI DMA 寄存器映射探针')
    print('=' * 70)

    for ipname in ['dma_in', 'dma_out']:
        ip = getattr(ol, ipname)
        print('\n' + '#' * 70)
        print('#  %s' % ipname)
        print('#' * 70)

        base = snap(ip)
        print('\n[初始快照]')
        for name, off in CAND:
            print('    0x%02X  %-14s = 0x%08X' % (off, name, base[off]))

        # 逐位写入，每次写完读全部偏移
        for bit, label in [(0x1, 'bit0'), (0x2, 'bit1'), (0x4, 'bit2'),
                           (0x1000, 'bit12')]:
            print('\n[写 0x%04X (%s) 到全部候选偏移，看谁跟着变]' % (bit, label))
            # 先全部清一次
            for _, off in CAND:
                ip.write(off, 0)
            changed = {}
            for wname, woff in CAND:
                # 只往这一个偏移写
                ip.write(woff, bit)
                after = snap(ip)
                # 找哪些偏移的值变了
                hits = [(n, o, after[o]) for n, o in CAND if after[o] != 0]
                ip.write(woff, 0)          # 还原
                # 判断：写入 woff 后，是否在别处也出现相同值
                changed[wname] = hits
                if hits:
                    desc = ', '.join('%s(0x%02X)=0x%X' % (n, o, v) for n, o, v in hits)
                    print('    写 %-14s(0x%02X) → 变化: %s' % (wname, woff, desc))
                else:
                    print('    写 %-14s(0x%02X) → 无任何偏移变化（写不进去）'
                          % (wname, woff))

    print('\n' + '=' * 70)
    print('  怎么读这份结果')
    print('=' * 70)
    print('''
  · 正常的 AXI DMA：往 MM2S_DMACR(0x00) 写 0x1，应只有 0x00 读回 0x1
  · 若写 0x00 却让 **别的偏移** 变化，说明地址有整体偏移
  · 若写哪个偏移都读不回，说明这个 IP 的 AXI-Lite 根本没连到 PS

  ⚠ 本脚本会临时改写 DMA 寄存器，跑完建议重新加载 overlay 再跑正式测试：
     sudo -E /usr/local/share/pynq-venv/bin/python3 -c "from pynq import Overlay; Overlay('/home/xilinx/gesture_system.bit')"
''')


if __name__ == '__main__':
    sys.exit(main())
