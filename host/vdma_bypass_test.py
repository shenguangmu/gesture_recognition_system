#!/usr/bin/env python3
"""
vdma_bypass_test.py —— 找出在本版 PYNQ 上访问 vdma 寄存器的可用方式

【背景】

`ol.vdma` 在 PYNQ 3.0.1 上直接抛：

    AttributeError: 'AxiVDMA' object has no attribute 's2mm_introut'

原因：PYNQ 的 `AxiVDMA` 专用驱动构造时要找中断，
而**本 BD 里所有中断都悬空**（没接 PS 的 IRQ_F2P，项目走轮询）。
于是专用驱动用不了。

本脚本**逐个尝试几种绕行方案**，报告哪个能读能写。
不猜 —— 测出来哪个能用就用哪个。

用法（板上）：
    sudo -E /usr/local/share/pynq-venv/bin/python3 vdma_bypass_test.py
"""

import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BIT = '/home/xilinx/gesture_system.bit'
VDMA_VER_OFF = 0x2C          # VDMA_VERSION，只读，用来看"A 方案是否真能读硬件"


def try_it(label, fn):
    """跑一个方案，打印结果或异常"""
    try:
        obj = fn()
        # 试着读版本寄存器 —— 能读出来才算真拿到 MMIO
        val = obj.read(VDMA_VER_OFF)
        print('  [ OK ] %-42s  读到 VERSION=0x%08X' % (label, val))
        return obj
    except Exception as e:
        print('  [FAIL] %-42s  %s: %s' % (label, type(e).__name__, e))
        return None


def main():
    from pynq import Overlay
    ol = Overlay(BIT)

    print('=' * 74)
    print('  vdma 绕行方案测试')
    print('=' * 74)

    # ---------- 先看 ip_dict 里 vdma 条目长什么样 ----------
    print('\n[1] ip_dict["vdma"] 的字段')
    info = ol.ip_dict['vdma']
    for k in sorted(info.keys()):
        v = info[k]
        if k == 'registers':
            v = '<%d 个寄存器>' % len(v)
        print('    %-14s = %s' % (k, v))

    # ---------- 方案 A：直接构造 DefaultIP ----------
    print('\n[2] 方案 A：从清理后的条目构造 DefaultIP')
    print('        （pop 掉 interrupts / driver 两个触发专用驱动的字段）')
    a_info = dict(info)
    popped = {k: a_info.pop(k, None) for k in ('interrupts', 'driver')}
    print('        pop 掉: %s' % {k: (v is not None) for k, v in popped.items()})
    objA = try_it('DefaultIP(清理后的条目)', lambda: _mk_default_ip(a_info))

    # ---------- 方案 B：不经 ip_dict，直接建 MMIO ----------
    print('\n[3] 方案 B：直接对 phys_addr 建 MMIO')
    base = info.get('phys_addr')
    rng = info.get('addr_range', 0x10000)
    print('        phys_addr=0x%08X  addr_range=0x%X' % (base, rng))
    objB = try_it('MMIO(0x%08X, 0x%X)' % (base, rng),
                  lambda: _mk_mmio(base, rng))

    # ---------- 方案 C：改 ip_dict 原地，再用 ol.vdma ----------
    print('\n[4] 方案 C：原地改 ip_dict 后访问 ol.vdma')
    try:
        ol.ip_dict['vdma'].pop('interrupts', None)
        ol.ip_dict['vdma'].pop('driver', None)
        # 清掉已缓存的 _ip_map，强制重建
        if hasattr(ol, '_ip_map') and hasattr(ol._ip_map, '_ip_dict'):
            ol._ip_map._ip_dict.clear()
        objC = try_it('ol.vdma（清缓存后）', lambda: ol.vdma)
    except Exception as e:
        print('  [FAIL] 方案 C                          %s: %s'
              % (type(e).__name__, e))
        objC = None

    # ---------- 汇总 ----------
    print('\n' + '=' * 74)
    print('  结论')
    print('=' * 74)
    ok = [n for n, o in (('A(DefaultIP)', objA), ('B(裸MMIO)', objB),
                         ('C(改ip_dict)', objC)) if o is not None]
    if ok:
        print('  ✅ 可用方案：%s' % '、'.join(ok))
        print('     优先用 %s（最直接、最少副作用）' % ok[0])
    else:
        print('  ❌ 三种方案都不行 —— 需要换思路')
        print('     （比如用 pynq.MMIO 直接映射，或改用 XRT 层）')

    # ---------- 顺带：写一个寄存器验证可写 ----------
    pick = objA or objB or objC
    if pick is not None:
        print('\n[5] 验证可写：往 S2MM_VDMACR(0x30) 写-读-还原')
        try:
            old = pick.read(0x30)
            pick.write(0x30, old | 0x1)
            got = pick.read(0x30)
            pick.write(0x30, old)
            print('    原值 0x%08X → 写 0x%08X → 读回 0x%08X → 还原 0x%08X'
                  % (old, old | 1, got, pick.read(0x30)))
            print('    %s' % ('✅ 可读可写' if got == (old | 1) else
                              '⚠ 写进去的值读回不一致'))
        except Exception as e:
            print('    写测试失败: %s' % e)


def _mk_default_ip(info):
    from pynq import DefaultIP
    return DefaultIP(info)


def _mk_mmio(base, rng):
    from pynq import MMIO
    return MMIO(base, rng)


if __name__ == '__main__':
    sys.exit(main())
