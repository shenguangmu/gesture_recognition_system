#!/usr/bin/env python3
"""
len_loopback.py —— 只测一件事：dma_in 的 LENGTH 寄存器能不能正确读写

【为什么单独测这个】

2026-09-21 实测发现：
    dma_in  LENGTH 写 614400 → 刚写完读回 **8192** ⚠
    dma_out LENGTH 写   9216 → 读回 9216 ✅

而 `dma_out` 正常说明 AXI-Lite 总线本身没问题 ——
所以要么是 dma_in 特有的问题，要么是"读的时机"问题。

**本脚本不启动任何 DMA、不碰 IP**，只做寄存器写-读回环。
这样就能把"传输导致的递减"和"根本写不进去"分开。

用法（板上）：
    sudo -E /usr/local/share/pynq-venv/bin/python3 len_loopback.py
"""

import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BIT = sys.argv[1] if len(sys.argv) > 1 else '/home/xilinx/gesture_system.bit'

# AXI DMA 各通道的寄存器
REG = {
    'dma_in  MM2S': {'CR': 0x00, 'SR': 0x04, 'ADDR': 0x18, 'LEN': 0x28},
    'dma_out S2MM': {'CR': 0x30, 'SR': 0x34, 'ADDR': 0x48, 'LEN': 0x58},
}


def main():
    from pynq import Overlay
    ol = Overlay(BIT)

    print('=' * 70)
    print('  len_loopback —— LENGTH 寄存器纯回环（不启动传输）')
    print('=' * 70)

    for name, r in REG.items():
        ip = ol.dma_in if name.startswith('dma_in') else ol.dma_out
        print('\n' + '#' * 70)
        print('#  %s' % name)
        print('#' * 70)

        # ---- [A] 确保通道停止 ----
        ip.write(r['CR'], 0)              # RS=0
        time.sleep(0.05)
        print('\n[A] 通道已停：DMACR=0x%08X  DMASR=0x%08X'
              % (ip.read(r['CR']), ip.read(r['SR'])))

        # ---- [B] 写不同长度，立刻读回 ----
        print('\n[B] 写 LENGTH → 立刻读回（通道停止状态下）')
        print('    %-12s %-12s %-12s %s' % ('写入', '读回', '差值', '判定'))
        for val in (614400, 100000, 123456, 8192, 9216, 4096, 65536):
            ip.write(r['LEN'], val)
            got = ip.read(r['LEN'])
            diff = val - got
            verdict = 'OK' if got == val else '⚠ 不符'
            print('    %-12d %-12d %-12d %s' % (val, got, diff, verdict))

        # ---- [C] 连续读三次，看会不会自己变 ----
        ip.write(r['LEN'], 614400)
        reads = [ip.read(r['LEN']) for _ in range(3)]
        print('\n[C] 写 614400 后连读三次: %s' % reads)
        print('    %s' % ('稳定' if len(set(reads)) == 1 else '⚠ 会变 —— 说明有东西在动'))

        # ---- [D] 地址寄存器也测一下（同样不该依赖传输） ----
        ip.write(r['ADDR'], 0x16A00000)
        print('\n[D] ADDR 写 0x16A00000 → 读回 0x%08X  %s'
              % (ip.read(r['ADDR']),
                 'OK' if ip.read(r['ADDR']) == 0x16A00000 else '⚠ 不符'))

    print('\n' + '=' * 70)
    print('  判读')
    print('=' * 70)
    print('''
  通道**停止**状态下：

  · 写什么读回什么   → 寄存器正常。之前的 8192 是**传输开始后**读到的
                       剩余量，属正常语义。
  · 写 614400 读回 8192（且固定不变）
                     → **长度真的没装载进去** —— 这才是 bug，
                       DMA 只搬 8192 字节，IP 喂不满，整条链卡死。

  ⚠ 若两个 DMA 表现一致，说明之前的差异来自"读的时机"，不是 IP 差异。
''')


if __name__ == '__main__':
    sys.exit(main())
