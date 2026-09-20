#!/usr/bin/env python3
"""
pynq_serial.py —— PYNQ-Z2 串口控制台（在 **PC** 上跑）

【这个脚本解决什么问题】

`docs/board-bringup-guide.md` §3.1 推荐 MobaXterm 当串口终端，理由很硬
（串口 + SSH + SFTP 三合一）。本脚本**不取代它**，只补一件 MobaXterm
不做的事：**从启动日志里把 IP 自动抠出来**。

为什么这件事值得单独做：

  §3.3 要你「看网口 IP（PC 与板子同网段）」然后去开 `http://<板子IP>:9090`。
  但 PYNQ 的 IP 也可能是 DHCP 分下来的，**不以 192.168.2.99 为准** ——
  于是每次都要在一屏启动日志里翻那个 `eth0: <BROADCAST,...>` 行，
  肉眼找一个四位数字组。启动日志滚得快，翻过去就得按 Reset 重来。

  本脚本边收边匹配，**退出时把本次会话里出现过的 IP 汇总打印**，
  并直接拼好 `http://<ip>:9090`。

【怎么用】

    python host/pynq_serial.py                # 先列端口，确认板卡是哪个 COM
    python host/pynq_serial.py COM7           # 连（默认 115200-8-N-1）
    python host/pynq_serial.py --auto         # 自动挑端口（排除蓝牙）

    会话里： Ctrl+C 退出。**所有按键原样透传**给板卡 ——
    它就是个普通串口终端，只是多做了「日志里找 IP」这一件事。

【⚠ 两个坑，都是这台机器上实际存在的】

  1) **蓝牙串口是幻影口。** Windows 上蓝牙 SPP 会占用 COM3/COM4 这类
     低编号端口，列表里看着就是「COM3 / COM4」，长得和板卡一模一样。
     选错了会以为是板卡没反应。本脚本按 InstanceId 里的 `BTHENUM`
     把它们标出来，`--auto` 时直接跳过。
     ⚠ 真正的板卡是 FTDI 芯片（PYNQ-Z2 的 Micro-USB 走 FTDI），
       描述里通常是 "USB Serial Port"。**看不到它，先查驱动。**

  2) **中文 Windows 控制台是 GBK。** 打 `✓`(U+2713) 会
     `UnicodeEncodeError` 直接崩 —— 这条 `host/README.md` 里记过。
     本脚本统一用 ASCII `[ OK ]` / `[ !! ]`，并且和 `bringup_check.py`
     一样把 stdout 重包成 UTF-8 + errors='replace' 兜底。

【判定】

  插上板卡 Micro-USB(J8)、上电、按一下板卡 Reset 键 →
  日志应刷到 `login:` 提示符。退出后若打印出 IP 列表，
  逐个往浏览器里试 `:9090`，**出 Jupyter 登录页的那个就是板卡地址**。

  ⚠ 一个 IP 都没抓到 = 板子可能没起完，或网线没插。
    先看日志滚到哪一步（卡在 `Loading kernel...` → 回 §1.4 查 DDR 参数）。

【依赖】

    pip install --user pyserial
"""

import argparse
import io
import re
import sys
import time

# ⚠ 与 bringup_check.py 同一套兜底：中文 Windows 控制台是 GBK，
#   打印非 GBK 字符会崩。用 errors='replace' 而不是 strict ——
#   宁可显示成 ?，也不要崩在半路。
#
# ⚠⚠ 但**必须加"还没包过"的判断** —— 这是 import 本模块的调用方会踩的坑：
#
#   本模块是**库 + 脚本**两用的（别的脚本会 `import pynq_serial`）。
#   如果**无条件**包一层，就会出现这样的顺序：
#
#       调用方先包一层  →  sys.stdout = W1(裸buffer)
#       再 import 本模块 →  W2 = W1.buffer 又包一层
#
#   两层 wrapper 共享同一个底层 buffer。**其中一层被 GC 回收时会把
#   buffer 一起关掉**，于是调用方最后打印汇总时抛：
#       ValueError: I/O operation on closed file
#
#   实测过：`python host/test_pynq_serial.py`（不加 PYTHONIOENCODING）
#   必崩，加上 `PYTHONIOENCODING=utf-8` 就正常 —— 因为那种情况下
#   调用方不会去包第一层。
#
#   **所以：只在自己是"最外层"时才包。** 已经有人包过就别动。
if getattr(sys.stdout, 'encoding', '').lower() not in ('utf-8', 'utf8'):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                      errors='replace')
    except Exception:
        pass
if getattr(sys.stderr, 'encoding', '').lower() not in ('utf-8', 'utf8'):
    try:
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8',
                                      errors='replace')
    except Exception:
        pass

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("  需要 pyserial： pip install --user pyserial")
    sys.exit(2)

# ⚠ 蓝牙 SPP 占的幻影口。Windows 上按 InstanceId 判定最可靠
#   （描述文字在不同系统语言下不一样，InstanceId 里的 BTHENUM 是稳定的）。
_BT_PATTERN = re.compile(r'BTHENUM', re.I)

# 板卡地址的形状：IPv4，排除回环 / APIPA / 广播 / 全零。
_IP_RE = re.compile(r'\b(\d{1,3}(?:\.\d{1,3}){3})\b')

# ⚠⚠ `ifconfig` 一行里会同时出现**好几个** IPv4：
#
#     inet 172.20.10.3  netmask 255.255.255.240  broadcast 172.20.10.15
#          ^^^^^^^^^^^ 这是板卡地址        ^^^^^^^^^^^^^^^ 这是掩码，不是地址！
#
# 只按"排除 .255 / 127. / 169.254."来筛是**不够的** —— 掩码和广播地址
# 都是合法 IPv4，会一起被留下。2026-09-19 实测：一行 DHCP 输出
# 会挑出 **3 个** 候选，其中 2 个是假的。
#
# 后果很具体：板到那天拿着 `255.255.255.240` 去开 `http://<ip>:9090`，
# 打不开，然后开始怀疑板子 —— **而这正是本脚本要消灭的那种浪费**。
#
# 所以先**按关键字切掉**这些字段，再抓 IP：
#   netmask/掩码 后面跟的值、broadcast/brd 后面跟的值，都不是板卡地址。
_NOT_AN_ADDR = re.compile(
    r'\b(?:netmask|broadcast|brd|Mask|掩码)\b[:\s]*\S+', re.I)

# MobaXterm 不做、本脚本专做的一件事：从日志里认 IP 所在的那一行。
_IP_LINE_RE = re.compile(r'\b(?:eth0|inet\s)', re.I)


def _is_board_port(p):
    """判定一个串口是不是板卡（而非蓝牙幻影口）"""
    blob = "%s %s %s %s" % (p.device, p.hwid,
                            p.description or '', p.manufacturer or '')
    return not _BT_PATTERN.search(blob)


def _pick_ips(text):
    """从一段文本里挑出像板卡地址的 IP，保持出现顺序、去重

    ⚠ 先切掉 netmask / broadcast 字段 —— 见 `_NOT_AN_ADDR` 的说明。
      不切的话，一行 DHCP 输出会挑出 3 个候选，其中 2 个是掩码和广播地址。
    """
    cleaned = _NOT_AN_ADDR.sub(' ', text)

    out = []
    for ip in _IP_RE.findall(cleaned):
        if ip.startswith('127.') or ip.startswith('169.254.'):
            continue          # 回环 / APIPA（没拿到 DHCP 时的自分配地址）
        if ip == '0.0.0.0' or ip.endswith('.255'):
            continue          # 未配置 / 广播
        if ip not in out:
            out.append(ip)
    return out


def list_ports_cmd():
    """不带参数跑：列出端口，标出哪个是幻影口"""
    ports = list_ports.comports()
    if not ports:
        print("  没发现任何串口。")
        print("  查：板卡 Micro-USB(J8) 插了没 · 板卡上电没 · FTDI 驱动装了没")
        return 2

    print("%-8s %-8s %s" % ("端口", "可用", "描述"))
    print("-" * 66)
    boards = []
    for p in ports:
        ok = _is_board_port(p)
        if ok:
            boards.append(p)
        # ⚠ 用 ASCII 标记，不用 ✓/✗ —— GBK 控制台会崩（见文件头 §2）
        print("%-8s %-8s %s" % (p.device, "[ OK ]" if ok else "[蓝牙]",
                                p.description))

    print()
    if not boards:
        print("  上面全是蓝牙幻影口，没有板卡。")
        print("  板卡是 FTDI 芯片，应该出现在描述为 'USB Serial Port' 的那一行。")
        print("  装了 FTDI 驱动或插好线之后再跑一次。")
        return 2

    print("  连板卡： python host/pynq_serial.py %s" % boards[0].device)
    return 0


def pick_auto():
    boards = [p for p in list_ports.comports() if _is_board_port(p)]
    if not boards:
        print("  没有可用串口。先不带参数跑一次看列表：")
        print("    python host/pynq_serial.py")
        sys.exit(2)
    if len(boards) > 1:
        print("  发现多个可用串口：%s —— 取第一个 %s"
              % (', '.join(p.device for p in boards), boards[0].device))
    return boards[0].device


def main():
    ap = argparse.ArgumentParser(
        description="PYNQ-Z2 串口控制台（边看日志边抠 IP）")
    ap.add_argument("port", nargs='?', help="串口名，如 COM7")
    ap.add_argument("-b", "--baud", type=int, default=115200,
                    help="波特率，默认 115200（PYNQ 镜像默认值）")
    ap.add_argument("--auto", action="store_true",
                    help="自动挑端口（跳过蓝牙幻影口）")
    args = ap.parse_args()

    if not args.port and not args.auto:
        return list_ports_cmd()

    port = args.port or pick_auto()

    try:
        ser = serial.Serial(port, args.baud, timeout=0.05)
    except serial.SerialException as e:
        print("  打不开 %s（%d）：%s" % (port, args.baud, e))
        print("  常见原因：端口号不对 / 被别的程序占用 / 板卡没上电。")
        print("  跑 `python host/pynq_serial.py` 看当前可用端口。")
        return 2

    print("  已连 %s @ %d-8-N-1   |   Ctrl+C 退出" % (port, args.baud))
    print("  （按板卡 Reset 键会重打一遍启动日志）")
    print("-" * 66)

    seen = []
    try:
        while True:
            n = ser.in_waiting
            data = ser.read(n if n else 1)
            if data:
                text = data.decode('utf-8', errors='replace')
                sys.stdout.write(text)
                sys.stdout.flush()
                seen.append(text)
                continue

            # 没数据时转发本地按键。⚠ Windows 用 msvcrt；
            #   这个脚本只在 PC 上跑，Linux 分支不写（见文件头）。
            try:
                import msvcrt
            except ImportError:
                continue

            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ('\r', '\n'):
                    ser.write(b'\r')
                elif ch == '\x08':              # 退格
                    ser.write(b'\x08 \x08')
                elif ch == '\x03':              # Ctrl+C 透传给板卡
                    ser.write(b'\x03')
                else:
                    ser.write(ch.encode('utf-8', errors='replace'))
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()

    # ── 收尾：把本次会话里抓到的 IP 汇总 ──
    all_text = ''.join(seen)
    # ⚠ 优先只看带 eth0 / inet 的行：启动日志里还有别的 IP 形状的串
    #   （版本号、时间戳、MAC 相关），全量扫容易误报。
    lines = [l for l in all_text.splitlines() if _IP_LINE_RE.search(l)]
    ips = _pick_ips('\n'.join(lines)) if lines else _pick_ips(all_text)

    print("\n" + "-" * 66)
    if ips:
        print("  本次会话识别到的 IP：")
        for ip in ips:
            print("    %-16s ->  http://%s:9090" % (ip, ip))
        print()
        print("  逐个往浏览器里试，出 Jupyter 登录页的那个就是板卡。")
        print("  （账号 xilinx / 密码 xilinx）")
    else:
        print("  没识别到 IP。")
        print("  按板卡 Reset 键让启动日志重打一遍；或检查网线插了没。")
        print("  若日志卡在 'Loading kernel...' → 回 §1.4 查 DDR 参数。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
