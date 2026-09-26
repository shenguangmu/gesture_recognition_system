#!/usr/bin/env python3
"""roi_analysis.py —— ROI 机制与类别可分性的实测工具

【解决什么问题】

「能分几个手势？」这个问题**不能靠推理**回答 —— 它取决于两个实测值：

    类间差异（不同手势之间差多少）
    ─────────────────────────────  =  信噪比
    类内波动（同一手势、不同条件下变多少）

信噪比低，加多少类都会混。本脚本就测这两个量。

【⚠ 一个必须避开的测试陷阱（踩过）】

要测「同一手势在不同条件下变多少」，正确做法是：
**用手部 mask 把手抠出来、贴到同一张背景板的不同位置**。

**不能**用 `np.roll` / 平移整幅图 —— 那样空出来的边是**纯黑（Y=0）**，
而前景判据是 `|Y - bg| > 25`，黑边自己就满足 `|0-32| = 32 > 25`，
于是黑边被判成前景、bbox 铺满画面。**会得出完全错误的结论。**
（2026-09-26 实际发生过：据此错判"ROI 跟随有 bug"，实际跟随是好的。）

【用法】

    # 有数据集时：每类一个子目录
    python host/roi_analysis.py --dir my_gestures/

    # 或者直接给几张图（每张当一个类别）
    python host/roi_analysis.py --images a.png b.png c.png

    # 调扰动幅度
    python host/roi_analysis.py --dir g/ --shift 40 --scale 0.10

【输出】

    ① 每个手势的基线（非零像素 / ROI）
    ② 类内波动：平移 / 缩放 / 亮度 各让输出变多少
    ③ 类间差异矩阵：两两手势之间差多少
    ④ 信噪比 → 类别数的量级判断

【⚠ 诚实边界】

  · 缩放是**合成**的（LANCZOS 缩放），不是真实远近变化
  · 全部走 **PC 侧 golden**（与硬件逐位一致，但不是硬件）
  · 结论只在**你给的那几张图**上成立；换采集条件要重测
"""

import argparse
import importlib.util
import os
import sys

import numpy as np

HOME = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HOME)

W, H = 640, 480
SIZE = 448                     # ROI 边长，与 auto_roi 默认一致


def _load(mod_name, relpath):
    spec = importlib.util.spec_from_file_location(mod_name,
                                                  os.path.join(ROOT, relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


G = _load('G', 'host/gesture_golden.py')
cf = _load('cf', 'host/capture_frame.py')
AR = _load('AR', 'host/auto_roi.py')

try:
    from PIL import Image
except ImportError:
    print("需要 Pillow：pip install pillow")
    sys.exit(1)


# ---------------------------------------------------------------------
def load_rgb(path):
    """读成 (480,640,3) RGB888。支持 .png/.jpg 和裸 .bin（RGB565）"""
    if path.lower().endswith('.bin'):
        d = np.fromfile(path, dtype='<u2').reshape(H, W)
        r = ((d >> 11) & 0x1F).astype(np.uint8) << 3
        g = ((d >> 5) & 0x3F).astype(np.uint8) << 2
        b = (d & 0x1F).astype(np.uint8) << 3
        return np.dstack([r, g, b])
    im = np.array(Image.open(path).convert('RGB'))
    return cf.fit_to_frame(im, W, H)      # ⚠ 与 capture_frame 同一套 cover 语义


def hand_mask(rgb):
    """手部 mask（走 auto_roi 的同一套判据）"""
    r565 = cf.rgb888_to_rgb565(np.ascontiguousarray(rgb))
    gray = AR.rgb565_to_gray(r565.astype('<u2'))
    bg = AR.estimate_background(gray, AR.DEFAULT_BORDER)
    return AR.foreground_mask(gray, bg, AR.DEFAULT_THRESH)


def make_plate(rgb, mask):
    """把手擦掉，得到一张干净背景板（用于把别处的手贴回来）"""
    out = rgb.copy()
    v = rgb[~mask]
    if len(v) == 0:
        return out
    out[mask] = np.median(v, axis=0).astype(np.uint8)
    return out


def render(plate, rgb, mask, dx=0, dy=0, scale=1.0, bright=0):
    """把（mask 抠出的）手贴回背景板 —— **背景不动**，只有手动"""
    out = plate.copy()
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return out
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    sub = rgb[y0:y1 + 1, x0:x1 + 1]
    msub = mask[y0:y1 + 1, x0:x1 + 1]

    if scale != 1.0:
        nh = max(1, int(sub.shape[0] * scale))
        nw = max(1, int(sub.shape[1] * scale))
        sub = np.array(Image.fromarray(sub).resize((nw, nh), Image.LANCZOS))
        msub = np.array(Image.fromarray((msub * 255).astype(np.uint8))
                        .resize((nw, nh), Image.NEAREST)) > 127
    if bright:
        sub = np.clip(sub.astype(np.int16) + bright, 0, 255).astype(np.uint8)

    cy = (y0 + y1) // 2 + dy
    cx = (x0 + x1) // 2 + dx
    ny0 = cy - sub.shape[0] // 2
    nx0 = cx - sub.shape[1] // 2
    ys2, xs2 = np.where(msub)
    for yy, xx in zip(ys2, xs2):
        Y, X = ny0 + yy, nx0 + xx
        if 0 <= Y < out.shape[0] and 0 <= X < out.shape[1]:
            out[Y, X] = sub[yy, xx]
    return out


def feature(rgb, size=SIZE):
    """走硬件同款链路：auto_roi 定 ROI（跟随手）→ gesture_preproc → 96x96"""
    r565 = cf.rgb888_to_rgb565(np.ascontiguousarray(rgb))
    res = AR.compute_roi(r565.astype('<u2'), size=size)
    roi = (res['roi_x'], res['roi_y'], res['roi_w'], res['roi_h'])
    return G.gesture_preproc(r565, W, H, *roi, thresh_mode=1), roi


def dist(a, b):
    """两张二值图的差异比例（汉明距离 / 总像素）；0=相同，1=完全相反"""
    return float(np.mean(a != b))


# ---------------------------------------------------------------------
def collect(args):
    """→ [(标签, 路径)]"""
    out = []
    if args.dir:
        for label in sorted(os.listdir(args.dir)):
            d = os.path.join(args.dir, label)
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.bin')):
                    out.append((label, os.path.join(d, f)))
    for p in (args.images or []):
        out.append((os.path.splitext(os.path.basename(p))[0], p))
    return out


def main():
    ap = argparse.ArgumentParser(description='ROI 机制与类别可分性实测')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--dir', help='数据集目录（每类一个子目录）')
    src.add_argument('--images', nargs='+', help='直接给几张图，每张当一个类')
    ap.add_argument('--shift', type=int, default=40, help='平移扰动幅度 px（默认 40）')
    ap.add_argument('--scale', type=float, default=0.10, help='缩放扰动比例（默认 0.10）')
    ap.add_argument('--bright', type=int, default=20, help='亮度扰动幅度（默认 20）')
    args = ap.parse_args()

    items = collect(args)
    if not items:
        print("没有找到图片。")
        return 1

    print("=" * 78)
    print("  ROI 机制与类别可分性实测")
    print("=" * 78)
    print("  ROI 策略：auto_roi 跟随（边长 %d）   thresh_mode = 1（二值）" % SIZE)
    print("  ⚠ 扰动是**合成**的；结论只在给的这几张图上成立")

    # ---- 载入 + 基线 ----
    data = {}
    print()
    print("  ① 基线")
    print("  " + "-" * 74)
    for label, path in items:
        rgb = load_rgb(path)
        mk = hand_mask(rgb)
        nz = int(mk.sum())
        if nz == 0:
            print("  %-18s ⚠ 没检测到手，跳过" % label)
            continue
        f, roi = feature(rgb)
        data[label] = dict(rgb=rgb, mask=mk, plate=make_plate(rgb, mk), base=f)
        print("  %-18s 手 %5d px   ROI=(%3d,%3d)   输出非零 %4d/9216"
              % (label, nz, roi[0], roi[1], int((f != 0).sum())))

    if len(data) < 1:
        print("\n  没有可用样本。")
        return 1

    # ---- 类内波动 ----
    print()
    print("=" * 78)
    print("  ② 类内波动（同一手势，只改采集条件 —— 手/背景分离保证测试有效）")
    print("=" * 78)
    within = {}
    for label, d in data.items():
        ds = []
        for v in (-args.shift, -args.shift // 2, args.shift // 2, args.shift):
            if v:
                ds.append((dist(d['base'], feature(render(d['plate'], d['rgb'], d['mask'], dx=v))[0]), 'x%+d' % v))
                ds.append((dist(d['base'], feature(render(d['plate'], d['rgb'], d['mask'], dy=v))[0]), 'y%+d' % v))
        for v in (1 - args.scale, 1 + args.scale):
            ds.append((dist(d['base'], feature(render(d['plate'], d['rgb'], d['mask'], scale=v))[0]), '缩放 x%.2f' % v))
        for v in (-args.bright, args.bright):
            ds.append((dist(d['base'], feature(render(d['plate'], d['rgb'], d['mask'], bright=v))[0]), '亮度 %+d' % v))
        ds.sort(reverse=True)
        within[label] = ds
        print("\n  【%s】" % label)
        for v, l in ds[:3]:
            print("      最敏感: %-14s %5.1f%%" % (l, 100 * v))
        print("      中位 %.1f%%    最大 %.1f%%"
              % (100 * np.median([x[0] for x in ds]), 100 * ds[0][0]))

    # ---- 类间差异 ----
    labels = list(data.keys())
    print()
    print("=" * 78)
    print("  ③ 类间差异矩阵（数字越大越容易分）")
    print("=" * 78)
    if len(labels) < 2:
        print("  ⚠ 只有 1 个类别，无法算类间差异。至少给 2 类。")
        cross_min = None
    else:
        pad = max(len(x) for x in labels) + 2
        print("  " + " " * pad + "".join("%-9s" % x[:8] for x in labels))
        vals = []
        for a in labels:
            row = "  %-*s" % (pad, a[:pad - 2])
            for b in labels:
                if a == b:
                    row += "%-9s" % "—"
                else:
                    v = dist(data[a]['base'], data[b]['base'])
                    row += "%-9.1f" % (100 * v)
                    vals.append(v)
            print(row)
        cross_min = min(vals) if vals else None
        print()
        print("  最小类间差异 = %.1f%%   （最像的那一对，决定上限）"
              % (100 * cross_min))

    # ---- 结论 ----
    print()
    print("=" * 78)
    print("  ④ 信噪比")
    print("=" * 78)
    wmax = max(within[k][0][0] for k in within)
    wmed = max(np.median([x[0] for x in within[k]]) for k in within)
    print("  类内波动  中位 %.1f%%   最大 %.1f%%" % (100 * wmed, 100 * wmax))
    if cross_min is not None:
        print("  类间差异  最小 %.1f%%（最像的那对）" % (100 * cross_min))
        print()
        print("  信噪比（类间最小 / 类内中位）= %.2f" % (cross_min / wmed))
        print()
        print("  ⚠ 判读（**仅供参考**，别当硬判据）：")
        print("     > 3  ：这些类别在该特征空间里分得比较开")
        print("     1~3  ：偏紧，建议减少类别数或增大类间形态差异")
        print("     < 1  ：最像的那两类基本分不开")
        print()
        print("  ⚠⚠ 本数字的局限：")
        print("     · 只用你给的 %d 张图算的；类内波动是**合成扰动**" % len(items))
        print("     · 真实类内波动还包含：不同人的手、真实光照、透视变化 —— 都**没测**")
        print("     · 所以真实信噪比会**比这里低**")
    else:
        print("  （类间差异不足，无法算信噪比）")

    return 0


if __name__ == '__main__':
    sys.exit(main())
