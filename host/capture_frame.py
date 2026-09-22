#!/usr/bin/env python3
"""
capture_frame.py —— 把一张图片（或 USB 摄像头的一帧）转成 PL 链路要的 RGB565 .bin

【为什么需要它】
  项目的输入契约是「**DDR 里一块 640×480 RGB565 = 614400 字节**」。
  没有摄像头时，需要有人把「普通图片」变成这个格式 —— 本脚本就是那一环。

  PL 侧的预处理链、DMA、DDR 通路**完全不用改** ——
  因为接口本来就是一块 DDR buffer，不关心数据从哪来。
  这样就把「图像来源」和「图像处理」彻底解耦了。

【两种用法】

  ① 静态图（任何时候都能跑，不需要硬件）
     python capture_frame.py --image photo.jpg --out frame.bin

  ② USB 摄像头（PYNQ 的 USB Host 口 + UVC 摄像头）
     python capture_frame.py --camera 0 --out frame.bin

【输出格式】（与 host/gesture_golden.py、dump_frame.py 一致）
  裸 .bin，无文件头；uint16 小端；行优先；640×480
  共 640*480*2 = 614400 字节

  ⚠ **RGB565 的位序**必须与 PL 侧一致，见下。

【⚠ 两个必须对齐的约定】

  1. **位序**：`R[15:11] G[10:5] B[4:0]`
     与 `src_hls/gesture_preproc.cpp` 的 rgb565→灰度 一致。
     搞反了症状是「颜色怪但图像结构对」——比全黑难查得多。

  2. **字节序**：本脚本写 **小端（little-endian）**，即低字节在前。
     ⚠ 这与 PL 侧 dvp_capture 的输出顺序必须一致。
     若发现图像「左右像素互换」，多半是这里的问题。
"""

import argparse
import io
import sys

# ⚠ Jupyter 兼容：IPython 的 sys.stdout 是 OutStream，没有 .buffer。
#    这个守卫必须在，否则 `%run capture_frame.py` 会崩。
if hasattr(getattr(sys.stdout, 'buffer', None), 'write') \
   and getattr(sys.stdout, 'encoding', '').lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import numpy as np

W, H = 640, 480


def rgb888_to_rgb565(rgb: np.ndarray) -> np.ndarray:
    """(H,W,3) uint8 → (H,W) uint16，位序 R[15:11] G[10:5] B[4:0]

    ⚠ 三个通道的位宽不同（5/6/5），**不能统一移位**。
      红蓝 >>3（到 5 位），绿 >>2（到 6 位）。
      写成统一 >>3 是最常见的错误 —— 症状是绿色偏暗。
    """
    r = rgb[:, :, 0].astype(np.uint16) >> 3   # 8 → 5 位
    g = rgb[:, :, 1].astype(np.uint16) >> 2   # 8 → 6 位
    b = rgb[:, :, 2].astype(np.uint16) >> 3   # 8 → 5 位
    return (r << 11) | (g << 5) | b


def fit_to_frame(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """把任意尺寸的图缩放/裁剪到 (h,w)。

    策略：**按比例缩放到"填满"，再居中裁剪**（cover）。
    理由：拉伸（stretch）会让手势变形，而变形的手直接影响
          后续 ROI 裁剪与形态学的效果；裁剪只损失边缘。
    """
    ih, iw = img.shape[:2]
    if ih == 0 or iw == 0:
        raise ValueError("输入图像尺寸为 0")

    scale = max(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))

    if (nw, nh) != (iw, ih):
        try:
            import cv2
            img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
        except ImportError:
            # 没有 cv2 时用 numpy 最近邻（够用，只是边缘略糙）
            yi = (np.arange(nh) * ih // nh).clip(0, ih - 1)
            xi = (np.arange(nw) * iw // nw).clip(0, iw - 1)
            img = img[yi][:, xi]

    # 居中裁剪
    y0 = (nh - h) // 2
    x0 = (nw - w) // 2
    return img[y0:y0 + h, x0:x0 + w]


def load_image(path: str, w: int, h: int) -> np.ndarray:
    """读图片 → (h,w,3) uint8 RGB。

    ⚠⚠ **不能用 `cv2.imread(path)` 直接读** —— 它在 Windows 上
       **读不了中文/非 ASCII 路径**（用系统 ANSI 编码打开文件），
       报 `can't open/read file: check file path/integrity`，
       而那个错误信息**完全没提编码**，很容易以为文件损坏。
       本机截图默认就带中文（「屏幕截图 2026-09-22 ....png」），必踩。

    正确做法：**自己读字节 + `cv2.imdecode`**，绕开文件路径编码。
    """
    img = None
    try:
        import cv2
        # ⚠ 必须用 np.fromfile 读字节（它按 Unicode 路径处理），
        #   再交给 imdecode —— 不能让 cv2 自己去开文件。
        buf = np.fromfile(path, dtype=np.uint8)
        if buf.size == 0:
            raise FileNotFoundError("文件是空的或读不到: %s" % path)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError("cv2 解不了这个格式: %s" % path)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    except ImportError:
        try:
            from PIL import Image
            img = np.array(Image.open(path).convert("RGB"))
        except ImportError:
            raise SystemExit(
                "需要 cv2 或 Pillow 之一来读图片。\n"
                "  板子上： sudo -E /usr/local/share/pynq-venv/bin/pip3 install pillow\n"
                "  PC 上：  pip install opencv-python  或  pip install pillow")
    return fit_to_frame(img, w, h)


def grab_camera(index: int, w: int, h: int) -> np.ndarray:
    """从 USB 摄像头（V4L2/UVC）抓一帧。

    ⚠ PYNQ-Z2 是 USB **host-only** 口，且分析报告提醒过：
      「USB 摄像头需**供电充足的 hub**」—— 直接插可能供电不足。
    """
    try:
        import cv2
    except ImportError:
        raise SystemExit("摄像头模式需要 cv2：pip install opencv-python")
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise SystemExit(
            "打不开摄像头 %d。检查：\n"
            "  1) ls /dev/video*  有没有设备节点\n"
            "  2) v4l2-ctl --list-devices\n"
            "  3) 供电 —— PYNQ 的 USB 口可能带不动，试有源 hub" % index)
    try:
        # 多读几帧 —— 摄像头刚打开的头几帧常是黑的或过曝
        for _ in range(5):
            cap.read()
        ok, frame = cap.read()
        if not ok or frame is None:
            raise SystemExit("摄像头发不出帧")
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    finally:
        cap.release()
    return fit_to_frame(frame, w, h)


def main() -> int:
    ap = argparse.ArgumentParser(
        description='图片 / USB 摄像头 → PL 链路要的 RGB565 .bin')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--image', metavar='PATH', help='输入图片（jpg/png/…）')
    src.add_argument('--camera', type=int, metavar='N',
                     help='USB 摄像头序号（通常 0）')
    ap.add_argument('--out', metavar='PATH', required=True, help='输出的 .bin')
    ap.add_argument('--width', type=int, default=W)
    ap.add_argument('--height', type=int, default=H)
    ap.add_argument('--png', metavar='PATH',
                    help='顺便存一张 PNG 预览（确认裁剪/缩放对不对）')
    args = ap.parse_args()

    w, h = args.width, args.height

    if args.image:
        rgb = load_image(args.image, w, h)
        print("[输入] 图片 %s" % args.image)
    else:
        rgb = grab_camera(args.camera, w, h)
        print("[输入] USB 摄像头 #%d" % args.camera)

    print("       缩放/裁剪到 %dx%d（cover 策略：填满后居中裁）" % (w, h))

    rgb565 = rgb888_to_rgb565(rgb)
    data = rgb565.astype('<u2').tobytes()      # ⚠ 小端，见文件头

    expect = w * h * 2
    if len(data) != expect:
        raise SystemExit("字节数不对：%d，期望 %d" % (len(data), expect))

    with open(args.out, 'wb') as f:
        f.write(data)
    print("[输出] %s  (%d 字节 = %dx%d RGB565)" % (args.out, len(data), w, h))

    if args.png:
        try:
            from PIL import Image
            Image.fromarray(rgb).save(args.png)
            print("[预览] %s  ← **先看这张**，确认构图/裁剪对不对" % args.png)
        except ImportError:
            print("(跳过 PNG 预览：没装 Pillow)")

    print()
    print("下一步（PC 上对拍 golden）:")
    print("  python host/gesture_golden.py --input %s --width %d --height %d \\"
          % (args.out, w, h))
    print("         --out golden.bin --png golden.png")
    print()
    print("上板（Jupyter 里）:")
    print("  g.in_buf[:] = np.fromfile('%s', dtype=np.uint8)" % args.out)
    print("  g.in_buf.flush()   # ⚠ 漏刷会拿到旧数据，且不报错")
    print("  g.run_once(); g.show()")
    return 0


if __name__ == '__main__':
    sys.exit(main())
