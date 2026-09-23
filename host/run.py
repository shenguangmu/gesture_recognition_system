#!/usr/bin/env python3
"""
run.py —— 板上日常操作的一键入口

【解决什么】

每次板子上电后，都要在 Jupyter 里敲一长串：

    import os, sys
    os.chdir('/home/xilinx'); sys.path.insert(0, '/home/xilinx')
    from gesture_overlay import GesturePipeline
    import numpy as np
    g = GesturePipeline(); g.print_info(); g.setup_dma()
    g.config(roi_x=.., roi_y=.., roi_w=.., roi_h=..)
    g.in_buf[:] = np.fromfile(...); g.in_buf.flush()
    g.run_once(); g.show()

本脚本把这一串变成：

    %run /home/xilinx/run.py

**设计取向：只做基本操作和输出，不做断言。**
判定/校验交给 `bringup_check.py`（那个是给"验证"用的）；
这个是给"干活"用的 —— 跑一次、看一眼、走人。

【用法】（Jupyter 里）

    %run /home/xilinx/run.py
        # 交互式：列出可用输入让你选，然后跑一帧显示

【刻意不做的事】

- **不用 argparse** —— `%run x.py --frames 3` 时 IPython 把参数透传，
  没传参数会去解析 Jupyter 自己的 argv 而报错。**交互式菜单天然没有这个问题**。
- **不做断言** —— 想看判定的用 `bringup_check.py`。
  这里失败了就把原因原样打出来，不替你下结论。
- **不重新实现 overlay 逻辑** —— 全部复用 `gesture_overlay.GesturePipeline`，
  免得多出一份会分叉的实现。

⚠ 需要 `/home/xilinx/` 下有 `gesture_overlay.py` + `gesture_system.bit` + `.hwh`。
"""

import os
import sys
import time

# --- 保证能 import 到 gesture_overlay -------------------------------------
# ⚠ Jupyter 的工作目录默认是 ~/jupyter_notebooks，不是 ~/。
#   不切的话 `from gesture_overlay import ...` 会 ModuleNotFoundError。
HOME = '/home/xilinx'
if os.path.isdir(HOME) and not os.path.exists('gesture_overlay.py'):
    os.chdir(HOME)
if HOME not in sys.path:
    sys.path.insert(0, HOME)

import numpy as np  # noqa: E402

W, H = 640, 480
IN_BYTES = W * H * 2
OUT_SIZE = 96

# 默认 ROI —— 与 host/gesture_overlay.py 的默认值一致
ROI = dict(x=160, y=80, w=320, h=320)


def hr(t):
    print("\n" + "=" * 62)
    print("  " + t)
    print("=" * 62)


def ask(prompt, default):
    """带默认值的输入 —— 直接回车就用默认。"""
    try:
        s = input("%s [%s]: " % (prompt, default)).strip()
    except EOFError:
        # 非交互环境（比如被管道喂进来）—— 用默认值，别卡住
        print("(非交互，用默认 %s)" % default)
        return default
    return s if s else str(default)


def list_dir(path, exts):
    """列目录里指定后缀的文件（按修改时间倒序，最近的在前面）。"""
    if not os.path.isdir(path):
        return []
    out = []
    for f in os.listdir(path):
        if f.lower().endswith(exts):
            full = os.path.join(path, f)
            out.append((os.path.getmtime(full), full))
    return [p for _, p in sorted(out, reverse=True)]


# =====================================================================
#  输入准备：把用户选的东西变成 614400 字节的 RGB565
# =====================================================================
def to_rgb565_from_image(path):
    """图片 → (480,640) uint16 RGB565。

    ⚠ 直接复用 `capture_frame.py` 的函数，**不另写一套** ——
      那两处的 RGB565 位序/缩放语义必须逐位一致，
      分叉出第二份实现是"看着对、板上不对"的来源。
    """
    import importlib.util
    # capture_frame.py 在 host/ 下；板上通常把它放在同目录
    for cand in ('capture_frame.py',
                 os.path.join(HOME, 'capture_frame.py'),
                 os.path.join(HOME, 'host', 'capture_frame.py')):
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location('cf', cand)
            cf = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cf)
            img = cf.load_image(path, W, H)      # 读取 + cover 缩放到 640x480
            return cf.rgb888_to_rgb565(img)
    raise SystemExit(
        "找不到 capture_frame.py —— 图片输入需要它做缩放和 RGB565 转换。\n"
        "  把它放到 %s/ 下即可，或用板子上已有的 frame.bin。" % HOME)


def to_rgb565_from_bin(path, strict=True):
    """裸 .bin → (480,640) uint16 RGB565。"""
    data = np.fromfile(path, dtype=np.uint8)
    if data.size != IN_BYTES:
        msg = ("%s 是 %d 字节，期望 %d（640*480*2）"
               % (path, data.size, IN_BYTES))
        if strict:
            raise SystemExit("!! " + msg)
        print("!! " + msg)
    n = min(data.size, IN_BYTES) // 2
    return data[:n * 2].view(np.uint16).reshape(-1, W)[:H]


def to_rgb565_from_camera(index):
    """USB 摄像头 → (480,640) uint16 RGB565。

    ⚠ 逻辑与 `host/usb_camera_run.py` 相同（那里有详细说明：
      必须强制 MJPEG，USB2 带宽才够）。这里做精简版，
      若要更详细的协商诊断，直接用那个脚本。
    """
    import cv2
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        raise SystemExit(
            "打不开摄像头 %d。查：\n"
            "  ls /dev/video*   /   lsusb   /   换个 USB 口" % index)
    try:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, H)
        aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fc = int(cap.get(cv2.CAP_PROP_FOURCC))
        got = "".join(chr((fc >> (8 * i)) & 0xFF) for i in range(4))
        print("  摄像头协商: %dx%d  %s" % (aw, ah, got))
        for _ in range(5):          # 丢开头几帧（AGC 未收敛）
            cap.read()
        ok, frame = cap.read()
        if not ok or frame is None:
            raise SystemExit("摄像头发不出帧")
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # cover 缩放到 640x480
        ih, iw = rgb.shape[:2]
        if (iw, ih) != (W, H):
            s = max(W / iw, H / ih)
            nw, nh = int(round(iw * s)), int(round(ih * s))
            rgb = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA)
            y0, x0 = (nh - H) // 2, (nw - W) // 2
            rgb = rgb[y0:y0 + H, x0:x0 + W]
    finally:
        cap.release()
    r = rgb[:, :, 0].astype(np.uint16) >> 3
    g_ = rgb[:, :, 1].astype(np.uint16) >> 2
    b = rgb[:, :, 2].astype(np.uint16) >> 3
    return (r << 11) | (g_ << 5) | b


def pick_input():
    """列出手边可用的输入，让用户选。返回 (rgb565, 描述)。

    ⚠⚠ **只列"输入尺寸"的 .bin（614400 字节）**。

      /home/xilinx/ 下同时躺着两类 .bin：
        · 输入帧  640×480 RGB565 = **614400 字节**
        · 输出结果 96×96  灰度  = **9216 字节**
      两者后缀一样，**光看名字分不出来**。
      不过滤的话，随手选中一个 9216 的"输出"当输入，
      会以 "字节数不对" 结束 —— 这不是用户能预料的错。

      （2026-09-23 实测踩到：菜单第 1 项是 hw_synth.bin，一个 9KB 的输出。）
    """
    hr("选择输入")

    all_bins = list_dir(HOME, ('.bin',))
    inputs  = [p for p in all_bins if os.path.getsize(p) == IN_BYTES]
    outputs = [p for p in all_bins if os.path.getsize(p) == OUT_SIZE * OUT_SIZE]
    others  = [p for p in all_bins
               if os.path.getsize(p) not in (IN_BYTES, OUT_SIZE * OUT_SIZE)]
    imgs = list_dir(HOME, ('.jpg', '.jpeg', '.png', '.bmp', '.webp'))

    opts = []
    for p in inputs[:8]:
        opts.append(('bin', p, "输入帧  %s  (614400 B)"
                     % os.path.basename(p)))
    for p in imgs[:6]:
        opts.append(('img', p, "图片    %s  (自动缩放)"
                     % os.path.basename(p)))
    opts.append(('cam', 0, "USB 摄像头 (/dev/video0)"))
    opts.append(('path', None, "手动输入路径…"))

    if not inputs and not imgs:
        print("  (没找到输入尺寸的 .bin，也没有图片 —— 用摄像头或手动指定)")
    if outputs or others:
        # ⚠ 明确说清为什么它们不在列表里，否则用户会以为脚本"看不见"文件
        print("  -- 以下不当作输入（尺寸不符） --")
        for p in outputs[:4]:
            print("     %s  (%d B = 96x96 的**输出**，不是输入)"
                  % (os.path.basename(p), os.path.getsize(p)))
        for p in others[:4]:
            print("     %s  (%d B，既不 %d 也不 %d)"
                  % (os.path.basename(p), os.path.getsize(p),
                     IN_BYTES, OUT_SIZE * OUT_SIZE))
    print()

    for i, (_, _, label) in enumerate(opts, 1):
        print("  %2d) %s" % (i, label))

    try:
        sel = input("\n选一个 [1]: ").strip() or "1"
    except EOFError:
        print("(非交互，用默认 1)")
        sel = "1"
    try:
        kind, path, _ = opts[int(sel) - 1]
    except (ValueError, IndexError):
        raise SystemExit("!! 无效选择: %r" % sel)

    if kind == 'path':
        path = ask("路径", os.path.join(HOME, 'frame.bin')).strip()

    if kind == 'bin':
        print("\n-> 读 %s" % path)
        return to_rgb565_from_bin(path), path
    if kind == 'img':
        print("\n-> 转图片 %s" % path)
        return to_rgb565_from_image(path), path
    if kind == 'cam':
        print("\n-> 开摄像头")
        return to_rgb565_from_camera(0), "USB 摄像头"
    # 走到这说明选了"手动路径"，按后缀猜
    if not os.path.exists(path):
        raise SystemExit("!! 文件不存在: %s" % path)
    if path.lower().endswith('.bin'):
        return to_rgb565_from_bin(path), path
    return to_rgb565_from_image(path), path


def ask_roi():
    """问 ROI。回车用默认。"""
    print("\nROI（直接回车用默认 %d,%d %dx%d）"
          % (ROI['x'], ROI['y'], ROI['w'], ROI['h']))
    s = ask("  四个数，空格隔开", "%d %d %d %d"
            % (ROI['x'], ROI['y'], ROI['w'], ROI['h']))
    try:
        x, y, w, h = (int(v) for v in s.replace(',', ' ').split())
    except ValueError:
        print("!! 解析不了，用默认")
        return dict(ROI)
    if w < OUT_SIZE or h < OUT_SIZE:
        print("!! ROI 必须 >= %dx%d（按比例分配隐含除数），用默认" % (OUT_SIZE, OUT_SIZE))
        return dict(ROI)
    return dict(x=x, y=y, w=w, h=h)


# =====================================================================
#  主流程
# =====================================================================
def main():
    hr("板上日常运行")
    print("  cwd: %s" % os.getcwd())

    # ---- 1. 加载 overlay ----
    print("\n[1] 加载 overlay")
    from gesture_overlay import GesturePipeline
    g = GesturePipeline()
    g.print_info()          # 看 IP 认出来没有
    g.setup_dma()

    # ---- 2. 选输入 ----
    rgb565, desc = pick_input()
    print("  RGB565: %s  %d 字节" % (rgb565.shape, rgb565.nbytes))

    # ---- 3. ROI + 配置 ----
    roi = ask_roi()
    print("\n[3] 配置: ROI=(%d,%d) %dx%d" % (roi['x'], roi['y'], roi['w'], roi['h']))
    g.config(roi_x=roi['x'], roi_y=roi['y'], roi_w=roi['w'], roi_h=roi['h'])

    # ---- 4. 跑一帧 ----
    print("\n[4] 运行")
    # ⚠ 必须小端 —— DDR 与裸 .bin 都是小端。
    #   直接 `g.in_buf[:] = rgb565`（uint16 赋给 u1 缓冲）会 ValueError。
    data = rgb565.astype('<u2').tobytes()
    g.in_buf[:] = np.frombuffer(data, dtype=np.uint8)

    t0 = time.time()
    g.run_once()            # 内部会 flush 输入
    dt = time.time() - t0

    # ---- 5. 输出 ----
    out = g.get_result()
    hr("结果")
    print("  输入   : %s" % desc)
    print("  ROI    : (%d,%d) %dx%d" % (roi['x'], roi['y'], roi['w'], roi['h']))
    print("  耗时   : %.3f s" % dt)
    print("  输出   : %dx%d  取值 %s  非零 %d/%d"
          % (OUT_SIZE, OUT_SIZE, sorted(set(out.ravel().tolist())),
             int((out != 0).sum()), out.size))

    g.show()                # Jupyter 里出图

    # ---- 6. 要不要落盘（对拍用） ----
    try:
        save = input("\n把结果存成 .bin 吗？(对拍用) [y/N]: ").strip().lower()
    except EOFError:
        save = 'n'
    if save in ('y', 'yes'):
        p = ask("  存到", os.path.join(HOME, 'hw.bin')).strip()
        out.tofile(p)
        print("  已存: %s" % p)

    print("\n完成。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
