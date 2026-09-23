#!/usr/bin/env python3
"""
usb_camera_run.py —— USB(UVC) 摄像头 → PL 预处理链

【为什么有它】

DVP(PMOD-CAMERA) 通路卡在 SCCB 无 ACK，已不是关键路径。
USB 摄像头是**另一条**输入路径，它绕开了全部 DVP 硬件：

    DVP 方案: 摄像头 --DVP并行--> PL(dvp_capture) --> PL 预处理链 --> DDR
    USB 方案: 摄像头 --USB--> PS CPU --> DDR --> PL(dma_in) --> PL 预处理链 --> DDR
                                        ↑ 从"写进 DDR"这步起，与方案 A 完全相同

⚠ **PL 侧一行都不用改**。这正是把边界定成「DDR 里一块 640×480 RGB565」的价值。

【用法】（板上）

    sudo -E /usr/local/share/pynq-venv/bin/python3 usb_camera_run.py --probe
        # 只探测摄像头，不碰 PL。**先跑这个**。

    sudo -E /usr/local/share/pynq-venv/bin/python3 usb_camera_run.py
        # 采集一帧 → 喂进 PL 链 → 存输出

    sudo -E /usr/local/share/pynq-venv/bin/python3 usb_camera_run.py --frames 5
        # 连续跑 5 帧（看时序稳不稳）

    sudo -E /usr/local/share/pynq-venv/bin/python3 usb_camera_run.py --save-preview p.png
        # 顺便把摄像头原始画面存下来，肉眼确认取景

⚠ 需要 overlay 在同一目录（`gesture_system.bit` + `gesture_system.hwh`）。
"""

import argparse
import io
import os
import sys
import time

# ⚠ 这层 UTF-8 包装只在**真实控制台**需要，且必须加守卫。
#   Jupyter/IPython 的 sys.stdout 是 OutStream，**没有 .buffer**，
#   无条件包装会 AttributeError 崩掉 —— 而 %run 在 Jupyter 里是正常用法。
#   （板子是 Linux/UTF-8 本不需要；加它是为了能在 PC 上静态检查/试跑不崩。）
if hasattr(getattr(sys.stdout, 'buffer', None), 'write') \
   and getattr(sys.stdout, 'encoding', '').lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


# =====================================================================
#  常量：必须与 PL 侧一致（改这里就要同步 `src_hls/gesture_preproc.h`）
# =====================================================================
W, H = 640, 480          # 输入契约：640×480 RGB565
IN_BYTES = W * H * 2     # 614400
OUT_SIZE = 96            # 输出契约：96×96 uint8
OUT_BYTES = OUT_SIZE * OUT_SIZE


# =====================================================================
#  摄像头
# =====================================================================
def list_video_devices():
    """列出 /dev/video* —— 打不开时用来自证"设备在不在"。"""
    try:
        return sorted(d for d in os.listdir('/dev') if d.startswith('video'))
    except OSError:
        return []


def open_camera(index, fourcc='MJPG', w=W, h=H, verbose=True):
    """打开 USB 摄像头并**强制 MJPEG**。

    ⚠⚠ 为什么要显式设 MJPG（本脚本最重要的一行）

      PYNQ-Z2 是 **USB 2.0**。720p 下两种格式的带宽差一个数量级：

        YUYV(未压缩): 1280*720*2*30 = 55 MB/s   → 远超 USB2 实际带宽(~35MB/s)
        MJPEG(压缩) : 3~6 MB/s                  → 绰绰有余

      不设 FOURCC 的话，UVC 摄像头**默认常协商成 YUYV** ——
      结果是**分辨率被悄悄降到 320×240，或者帧率掉到个位数**，
      而且**不报任何错**。现象是"能出图但很糊/很卡"，很容易误判成别的问题。

      所以：**先设 FOURCC，再设分辨率**（顺序不能反 —— 有些驱动
      只有在 FOURCC 定下来之后才会接受目标分辨率）。

    返回 (cap, 实际协商到的宽, 高, 格式串)。
    """
    import cv2

    devs = list_video_devices()
    if verbose:
        print("  /dev/video* : %s" % (devs if devs else "(无)"))

    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)   # ⚠ 显式 V4L2，别让后端乱选
    if not cap.isOpened():
        cap.release()
        raise SystemExit(
            "打不开摄像头 %d。逐条排查：\n"
            "  1) ls /dev/video*      有没有设备节点（没有 = 没认到 USB 设备）\n"
            "  2) lsusb               看得到摄像头吗（看不到 = 线/供电/口的问题）\n"
            "  3) 换个 USB 口试试\n"
            "  4) dmesg | tail -20    看内核报什么\n"
            "  ⚠ 本板已用 12V DC 供电(J9=REG)，供电余量足够，一般不是供电问题。\n"
            "  ⚠ Jupyter 里跑要在最前面加 %%run 或用 ! 前缀；"
            "本脚本设计为命令行运行。" % index)

    # ---- ① 先设格式（顺序不能反）----
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
    # ---- ② 再设分辨率 ----
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)

    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fc = int(cap.get(cv2.CAP_PROP_FOURCC))
    got_fourcc = "".join(chr((fc >> (8 * i)) & 0xFF) for i in range(4))

    if verbose:
        print("  协商结果: %dx%d  FOURCC=%s" % (aw, ah, got_fourcc))

    # ⚠ 自检：格式没生效是最隐蔽的失败模式，必须报出来
    if fourcc == 'MJPG' and got_fourcc.strip() != 'MJPG':
        print("  !! 警告：请求 MJPG，实际是 %r —— 带宽可能不够（会降分辨率/掉帧）"
              % got_fourcc)
    if (aw, ah) != (w, h):
        print("  !! 警告：请求 %dx%d，实际 %dx%d —— 后续会缩放，但视野会变"
              % (w, h, aw, ah))
    return cap, aw, ah, got_fourcc


def grab_frame(cap, warmup=5):
    """抓一帧，返回 (H,W,3) RGB uint8。

    ⚠ 先丢几帧：摄像头刚打开的头几帧常是全黑/过曝（AGC 还没收敛）。
    """
    import cv2
    for _ in range(warmup):
        cap.read()
    ok, frame = cap.read()
    if not ok or frame is None:
        raise SystemExit("摄像头发不出帧（read() 返回失败）")
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


def fit_to_frame(img, w, h):
    """等比缩放 + 居中裁剪到 (h,w)（cover）。

    ⚠ **不要拉伸**（stretch）—— 拉伸会让手势变形，
      直接破坏后续的 ROI 裁剪与形态学效果。这里只用裁剪来适配。
    ⚠ 与 `host/capture_frame.py` 的 `fit_to_frame` **同一套语义** ——
      两边不一致会导致"PC 上看着对、板上不对"。
    """
    import cv2
    ih, iw = img.shape[:2]
    if (iw, ih) == (w, h):
        return img
    scale = max(w / iw, h / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    y0, x0 = (nh - h) // 2, (nw - w) // 2
    return img[y0:y0 + h, x0:x0 + w]


def rgb888_to_rgb565(rgb):
    """(H,W,3) uint8 → (H,W) uint16，位序 R[15:11] G[10:5] B[4:0]

    ⚠ 三个通道位宽不同（5/6/5），**不能统一移位**：
      红蓝 >>3（到 5 位），绿 >>2（到 6 位）。
      写成统一 >>3 是最常见的错误 —— 症状是**绿色偏暗**。

    ⚠ 与 `host/capture_frame.py` **逐位一致** —— 必须与 PL 侧
      `gesture_preproc.cpp` 的 rgb565→灰度 对齐，否则板上输出与 golden 对不上。
    """
    import numpy as np
    r = rgb[:, :, 0].astype(np.uint16) >> 3
    g = rgb[:, :, 1].astype(np.uint16) >> 2
    b = rgb[:, :, 2].astype(np.uint16) >> 3
    return (r << 11) | (g << 5) | b


def to_dma_bytes(rgb565):
    """(H,W) uint16 RGB565 → 614400 字节的 uint8 字节流（DMA 直接搬）。

    ⚠⚠ **必须小端**（与 `host/capture_frame.py:187` 同款写法）。

      裸 `.bin` 与 DDR 里的字节序都是小端。若图省事写成
          in_buf[:] = rgb565        # ✗ 直接赋 uint16 给 u1 缓冲
      会直接 ValueError（dtype 不匹配，numpy 不会替你转）。

      也不要用 `view([('lo','u1'),('hi','u1')])` 那种结构化写法 ——
      能跑，但**字节序取决于本机**，在大端机器上会静默字节序错乱。

      `.astype('<u2').tobytes()` 才是明确的：`<` 显式声明小端。
    """
    return rgb565.astype('<u2').tobytes()


# =====================================================================
#  主流程
# =====================================================================
def main():
    ap = argparse.ArgumentParser(
        description="USB(UVC) 摄像头 → PL 预处理链",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--probe', action='store_true',
                    help='只探测摄像头（不碰 PL）—— **第一次先跑这个**')
    ap.add_argument('--camera', type=int, default=0, help='摄像头序号（默认 0）')
    ap.add_argument('--frames', type=int, default=1, help='跑几帧（默认 1）')
    ap.add_argument('--fourcc', default='MJPG', help='像素格式（默认 MJPG）')
    ap.add_argument('--roi', type=int, nargs=4, metavar=('X', 'Y', 'W', 'H'),
                    default=[160, 80, 320, 320], help='ROI（默认 160 80 320 320）')
    ap.add_argument('--save-preview', metavar='PATH',
                    help='把摄像头原始帧存成 PNG（肉眼确认取景）')
    ap.add_argument('--out', metavar='PATH', help='把 PL 输出存成 .bin')
    args = ap.parse_args()

    # ---- 摄像头 ----
    print("=" * 66)
    print("USB 摄像头 → PL 预处理链")
    print("=" * 66)
    print("[1] 打开摄像头")
    cap, aw, ah, got = open_camera(args.camera, args.fourcc)
    try:
        print("\n[2] 抓一帧")
        t0 = time.time()
        rgb = grab_frame(cap)
        print("  原始帧 %s  用时 %.3f s" % (rgb.shape, time.time() - t0))

        if args.save_preview:
            try:
                import cv2
                cv2.imwrite(args.save_preview,
                            cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
                print("  预览已存: %s" % args.save_preview)
            except Exception as e:
                print("  预览保存失败: %s" % e)

        if args.probe:
            print("\n>>> --probe 模式：摄像头可用，未触碰 PL。")
            print("    确认上面没有 !! 警告之后，去掉 --probe 正式跑。")
            return 0

        # ---- 适配到契约尺寸 ----
        print("\n[3] 适配到 640x480（等比缩放 + 居中裁剪）")
        rgb = fit_to_frame(rgb, W, H)
        rgb565 = rgb888_to_rgb565(rgb)
        print("  RGB565 形状 %s  字节 %d（契约 %d）"
              % (rgb565.shape, rgb565.nbytes, IN_BYTES))
        assert rgb565.nbytes == IN_BYTES

        # ---- 起 overlay ----
        print("\n[4] 加载 overlay")
        from gesture_overlay import GesturePipeline
        g = GesturePipeline()
        g.print_info()
        g.setup_dma()
        g.config(roi_x=args.roi[0], roi_y=args.roi[1],
                 roi_w=args.roi[2], roi_h=args.roi[3])

        # ---- 跑帧 ----
        print("\n[5] 跑 %d 帧" % args.frames)
        times = []
        for i in range(args.frames):
            t0 = time.time()
            frame = rgb565 if i == 0 else rgb888_to_rgb565(
                fit_to_frame(grab_frame(cap), W, H))

            # ⚠ 与方案 A 完全相同的下游：写 DDR → flush → run
            data = to_dma_bytes(frame)
            if len(data) != IN_BYTES:
                raise SystemExit("字节数不对：%d，期望 %d" % (len(data), IN_BYTES))
            g.in_buf[:] = __import__('numpy').frombuffer(data, dtype='u1')
            g.run_once()          # run_once 内部会 flush 输入
            times.append(time.time() - t0)

            out = g.get_result() if hasattr(g, 'get_result') \
                else __import__('numpy').array(g.out_buf, dtype='u1').reshape(OUT_SIZE, OUT_SIZE)
            nz = int((out != 0).sum())
            print("  帧 %d: 输出非零 %d/%d  用时 %.3f s"
                  % (i + 1, nz, OUT_BYTES, times[-1]))

        out = __import__('numpy').array(g.out_buf, dtype='u1').reshape(OUT_SIZE, OUT_SIZE)
        print("\n[6] 结果")
        print("  输出 %dx%d  取值 %s  非零 %d"
              % (OUT_SIZE, OUT_SIZE, sorted(set(out.ravel().tolist()))[:5], (out != 0).sum()))
        print("  平均单帧 %.3f s（含采集+传输+处理）" % (sum(times) / len(times)))

        if args.out:
            out.tofile(args.out)
            print("  已存: %s" % args.out)

        print("\n>>> 完成。")
        return 0
    finally:
        cap.release()


if __name__ == '__main__':
    sys.exit(main())
