#!/usr/bin/env python3
"""
test_pynq_serial.py —— pynq_serial 的离线自检（不需要板子、不需要串口）

【为什么需要】

`pynq_serial.py` 板到当天要用，而它的核心逻辑是**从启动日志里认 IP** ——
认错了的后果很具体：拿着掩码地址去开 `http://<ip>:9090`，打不开，
然后开始怀疑板子。

这个 bug **真实存在过**（2026-09-19 发现）：一行 DHCP 输出
`inet 172.20.10.3 netmask 255.255.255.240 broadcast 172.20.10.15`
会被挑出 **3 个**候选，其中 2 个是掩码和广播地址。

所以这里把"认 IP"和"挑串口"这两件事钉住 —— 它们**不需要硬件**，
没理由因为没有板子就不测。

跑法：
    python host/test_pynq_serial.py
判定：`*** PYNO_SERIAL TESTS PASSED ***`
"""

import io
import sys
from pathlib import Path

# ⚠ 只在 stdout 还是 Windows 默认编码时才包一层 UTF-8。
#   无条件包会出问题：`pynq_serial` 自己也可能包过，
#   重复包装后内层被回收，最后打印汇总时抛
#   `ValueError: I/O operation on closed file` —— 测试反而挂在自己身上。
if getattr(sys.stdout, 'encoding', '').lower() not in ('utf-8', 'utf8'):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                      errors='replace')
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pynq_serial as ps   # noqa: E402

_p = _f = 0
_skipped = 0


def ck(cond, msg):
    global _p, _f
    if cond:
        _p += 1
    else:
        _f += 1
        print("    [FAIL] %s" % msg)
    return cond


# =====================================================================
def test_pick_ips():
    print("\n[1] 从启动日志里认 IP（本文件存在的首要理由）")

    cases = [
        # 名称, 日志片段, 期望结果
        ("标准 ifconfig 行",
         "eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc pfifo_fast\n"
         "        inet 192.168.2.99/24 brd 192.168.2.255 scope global eth0",
         ['192.168.2.99']),

        # ⚠ 这一条是真 bug 的回归 —— 见文件头
        ("DHCP 行（含 netmask/broadcast）",
         "inet 172.20.10.3 netmask 255.255.255.240 broadcast 172.20.10.15",
         ['172.20.10.3']),

        ("纯 inet 行",
         "    inet 10.0.0.7/24 brd 10.0.0.255 scope global dynamic eth0",
         ['10.0.0.7']),

        ("回环 / APIPA 必须被排除",
         "inet 127.0.0.1\ninet 169.254.3.4\ninet 192.168.1.50",
         ['192.168.1.50']),

        ("还没启动完（应返回空）",
         "Starting kernel ...\n[    0.000000] Booting Linux",
         []),

        ("多个真实地址（去重、保序）",
         "inet 192.168.2.99/24 brd 192.168.2.255\ninet 192.168.2.99/24",
         ['192.168.2.99']),
    ]

    for name, text, want in cases:
        got = ps._pick_ips(text)
        ck(got == want, "%s → %s（期望 %s）" % (name, got, want))

    # 掩码本身绝不该作为结果出现
    got = ps._pick_ips("inet 172.20.10.3 netmask 255.255.255.240")
    ck('255.255.255.240' not in got, "掩码 255.255.255.240 未被当成板卡地址")


def test_board_port():
    print("\n[2] 串口筛选（跳过蓝牙幻影口）")

    # ⚠ 不要写 `from serial.tools import list_ports; list_ports.ListPortInfo` ——
    #   在 pyserial 3.5 里 `ListPortInfo` **不在 list_ports 顶层**
    #   （它在 list_ports_common 里，且各平台实现可能再包一层）。
    #   本文件第一版就是这么写的，于是**本机有 pyserial 也照样静默跳过**，
    #   那 3 项从来没真跑过 —— 是"跳过计数"把它照出来的。
    #
    #   改成**问 comports() 要真实类型**：拿它返回的第一个对象
    #   （没有就退到 list_ports_common），总之与运行时用的是同一个类。
    PortInfo = None
    try:
        from serial.tools import list_ports
        got = list_ports.comports()
        if got:
            PortInfo = type(got[0])
        else:
            from serial.tools.list_ports_common import ListPortInfo
            PortInfo = ListPortInfo
    except Exception:
        PortInfo = None

    if PortInfo is None:
        # ⚠ 缺 pyserial 时**必须显式说"跳过 N 项"**，不能只默默少跑。
        #   否则汇总照样报 PASSED —— 覆盖率悄悄缩水而没人知道。
        #   （CI 首次跑这个 job 时就是这么红的：runner 没装 pyserial。）
        global _skipped
        _skipped = 3
        print("    [跳过] 本机没有 pyserial，这 3 项未执行")
        return

    def mk(dev, hwid, desc='', mfg=''):
        # ⚠ `ListPortInfo(device)` 的 device 是**必需参数**，
        #   不能 `ListPortInfo()` 再赋值 —— 那会 TypeError。
        #   （本测试第一版就是这么写的；因为一直在静默跳过，
        #     这个错从没暴露过，直到"跳过计数"把它照出来。）
        p = PortInfo(dev)
        p.hwid, p.description, p.manufacturer = hwid, desc, mfg
        return p

    # 真板卡：USB-SERIAL，不是蓝牙
    ck(ps._is_board_port(mk('COM7', 'USB VID:PID=0403:6001')),
       "COM7（USB 串口）判为板卡")
    ck(ps._is_board_port(mk('/dev/ttyUSB0', 'USB VID:PID=0403:6001')),
       "ttyUSB0 判为板卡")
    # 蓝牙幻影口：InstanceId 里有 BTHENUM（描述文字会随系统语言变，这个不会）
    ck(not ps._is_board_port(
        mk('COM5', r'BTHENUM\{00001101-0000-1000-8000-00805F9B34FB}_LOCALMFG',
           'Bluetooth 链接上的标准串行')),
       "蓝牙幻影口（BTHENUM）被排除")


# =====================================================================
def main():
    print("=" * 69)
    print("  pynq_serial 离线自检（不需要板子 / 串口）")
    print("=" * 69)

    test_pick_ips()
    test_board_port()

    print("\n" + "=" * 69)
    if _f == 0:
        msg = "  *** PYNQ_SERIAL TESTS PASSED ***  (%d 项" % _p
        if _skipped:
            msg += "，跳过 %d 项" % _skipped
        print(msg + ")")
    else:
        print("  *** PYNQ_SERIAL TESTS FAILED ***  (%d 通过, %d 失败)" % (_p, _f))
    print("=" * 69)
    return 1 if _f else 0


if __name__ == '__main__':
    sys.exit(main())
