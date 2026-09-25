#!/usr/bin/env python3
"""
push.py —— 把文件 / 图片推到 PYNQ 板

【为什么有它】

`docs/board-cheatsheet.md` §一 讲了四种传法（scp / Jupyter 上传 / SFTP /
HTTP），但每次手敲 `scp` 有三个反复出错的点：

  1. **图片得先转成 `.bin`** —— 板子不认 jpg/png，PL 的输入契约是
     「DDR 里一块 640×480 RGB565 = 614400 字节」。忘了这一步，
     传上去板子根本不认，而报错信息不会告诉你是格式问题。
  2. **ROI 与 golden 必须配套** —— 只传 frame.bin 不传 golden，
     到板上没法对拍，等于白传。
  3. **不核 md5** —— 传了一半 / 传了旧文件，板上跑出旧结果，
     现象极难往回追（2026-09-23 就因为这个绕了一大圈）。

本脚本把这三件事一起做了：**能转就转、能配就配、传完就核**。

【用法】

  # 推一张图片（自动转 RGB565 .bin + 自动算配套 golden + 传）
  python host/push.py 手.jpg

  # 指定 ROI（默认用 auto_roi 自动估算）
  python host/push.py 手.jpg --roi 160 80 320 320

  # 推任意文件（裸传，不转换）
  python host/push.py 固件.bit host/run.py

  # 推整个目录
  python host/push.py -r host/ --dest /home/xilinx

  # 只看会传什么，不实际传
  python host/push.py 手.jpg --dry-run

  # 只推 .bin（已经是 RGB565 了，不转换）
  python host/push.py frame.bin

【它不是万能的】

  ⚠ **ssh 必须免密**。若提示要密码，先在 PC 上跑一次
     `ssh-copy-id xilinx@<板子IP>`（或 `ssh xilinx@<IP>` 输一次密码）。
     本脚本**不处理交互式密码** —— 那会把"一键推送"变回手敲。

  ⚠ 不重新实现 RGB565 转换 —— 直接 import `capture_frame.py` 的函数。
     这个项目在"多份实现抄同一套错"上栽过一次（crop_scale 固定步长，
     HLS/C++/Python 三份一起错），所以**转换只有一份实现**。

  ⚠ 不重新实现 golden —— 直接调 `gesture_golden.py`。
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys

HOME = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HOME)

# 与 PL 侧一致的常量
W, H = 640, 480
IN_BYTES = W * H * 2          # 614400，RGB565
OUT_PIXELS = 96 * 96          # 9216，输出特征图

DEFAULT_BOARD = "xilinx@192.168.2.99"
DEFAULT_DEST = "/home/xilinx"

IMG_EXT = ('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff')


def _scp_env():
    """scp 的环境。

    ⚠⚠ Git Bash(MSYS) 会把**看起来像 Unix 路径的参数**自动转成 Windows 路径。
       于是 `scp x.bin board:/home/xilinx/` 里的 `/home/xilinx/` 在**本地**
       就被转成了 `C:/Program Files/Git/home/xilinx` —— 板子上根本没这个目录。
       报错长这样：

           scp: dest open "C:/Program Files/Git/home/xilinx/": No such file...

       ⚠ 这个转换**只针对命令行参数**，不影响 `-o` 之类的选项值，
         也不影响 `xilinx@ip:/path` 这种**带主机前缀**的写法（那本就带冒号）。
         栽的是 `--dest` 这种**裸路径参数**。

       MSYS2_ARG_CONV_EXCL='*' 关掉整个参数转换。
       在非 MSYS 环境（Linux/macOS/真 PowerShell）下这个变量无害。
    """
    env = os.environ.copy()
    env['MSYS2_ARG_CONV_EXCL'] = '*'
    env['MSYS_NO_PATHCONV'] = '1'          # 老版 MSYS 用的名字
    return env


# ---------------------------------------------------------------------
def md5_local(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            h.update(blk)
    return h.hexdigest()


def ssh(board, cmd, check=True):
    """在板子上跑一条命令，返回 (ok, stdout)

    ⚠ 同样要带 _scp_env() —— 命令行里含 `/home/...` 时 MSYS 也会去转。
    """
    p = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        board, cmd],
                       capture_output=True, text=True, env=_scp_env(),
                       encoding='utf-8', errors='replace')
    if check and p.returncode != 0:
        return False, (p.stderr or p.stdout).strip()
    return p.returncode == 0, (p.stdout or '').strip()


def check_board(board):
    ok, out = ssh(board, 'echo OK')
    if not ok:
        print("!! 连不上板子 %s" % board)
        if 'Permission denied' in out or 'password' in out.lower():
            print("   → ssh 要密码。先在 PC 上做一次免密：")
            print("       ssh-copy-id %s" % board)
            print("     本脚本刻意不处理交互式密码 —— 那就不叫一键了。")
        else:
            print("   → 检查：网线 / 板子是否开机 / IP 对不对")
            print("     找 IP：python host/pynq_serial.py")
        return False
    return True


# ---------------------------------------------------------------------
def crop_report(src):
    """报告这张图会被裁掉多少。

    ⚠ 为什么值得单独报：转换是 **cover**（按比例缩放到填满，再居中裁剪），
      裁掉的量取决于**宽高比**，而且**完全静默** —— 转出来的 .bin
      永远"尺寸正确"（614400 字节），你不会得到任何提示。

      实测（这里就是那张表）：

          16:9 手机横向 1920x1080  →  左右各裁 12.5%，共 25% 宽度
          4:3  相机     1600x1200  →  不裁
          3:2  单反     3000x2000  →  左右各裁 5.6%，共 11% 宽度
          1:1  方图     1000x1000  →  上下各裁 12.5%，共 25% 高度
          9:16 手机竖拍 1080x1920  →  上下各裁 28.9%，共 58% 高度 ⚠
          超宽          3000x600   →  左右各裁 36.7%，共 73% 宽度 ⚠⚠

    返回 (描述字符串, 裁掉的比例 0..1)；读不出尺寸时返回 (None, 0)
    """
    try:
        from PIL import Image
        with Image.open(src) as im:
            iw, ih = im.size
            mode = im.mode
    except Exception:
        return None, 0.0

    scale = max(W / iw, H / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    cw, ch = nw - W, nh - H

    if cw > 0 and cw >= ch:
        frac = cw / float(nw)
        msg = ("±%.1f%% 宽（共裁掉 %.0f%%）" % (100 * cw / 2.0 / nw, 100 * frac))
    elif ch > 0:
        frac = ch / float(nh)
        msg = ("±%.1f%% 高（共裁掉 %.0f%%）" % (100 * ch / 2.0 / nh, 100 * frac))
    else:
        frac, msg = 0.0, '无（比例正好 4:3 或更窄）'

    if 'A' in mode.upper():
        msg += '  ⚠ 这张图有透明通道 —— alpha 会被**忽略**，取底层 RGB'
    return "%dx%d → 居中裁剪 %s" % (iw, ih, msg), frac


def convert_image(src, out_bin, out_png=None):
    """图片 → 640x480 RGB565 .bin。

    ⚠ 复用 capture_frame.py 的函数，**不另写一套** —— 见文件头。
    """
    import importlib.util
    import numpy as np
    spec = importlib.util.spec_from_file_location(
        'cf', os.path.join(HOME, 'capture_frame.py'))
    cf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cf)

    img = cf.load_image(src, W, H)          # 读取 + cover 缩放到 640x480
    rgb565 = cf.rgb888_to_rgb565(img)
    rgb565.astype('<u2').tofile(out_bin)

    if out_png:
        try:
            from PIL import Image
            Image.fromarray(img).save(out_png)
            return os.path.getsize(out_bin), out_png
        except ImportError:
            pass
    return os.path.getsize(out_bin), None


def make_golden(frame_bin, gold_bin, roi):
    """调 gesture_golden.py 算配套 golden。"""
    cmd = [sys.executable, os.path.join(HOME, 'gesture_golden.py'),
           '--input', frame_bin,
           '--roi', *[str(v) for v in roi],
           '--out', gold_bin]
    # ⚠ 必须显式指定 encoding —— Windows 默认用 GBK 解子进程输出，
    #   而 kid 脚本会打 UTF-8 的中文/符号，撞上就 UnicodeDecodeError，
    #   而且报在**子线程**里（_readerthread），主流程只看到一段莫名其妙的
    #   traceback。2026-09-25 实测踩到。
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if p.returncode != 0:
        return False, (p.stderr or p.stdout).strip()
    return True, ''


def auto_roi_of(frame_bin):
    """调 auto_roi.py 估 ROI，返回 (x,y,w,h) 或 None。"""
    import json
    cmd = [sys.executable, os.path.join(HOME, 'auto_roi.py'),
           '--input', frame_bin, '--size', '448']
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    if p.returncode != 0:
        return None
    for ln in (p.stdout or '').splitlines():
        s = ln.strip()
        if s.startswith('ROI') and 'x=' in s:
            try:
                kv = dict(t.split('=') for t in s.split()[-4:])
                return (int(kv['x']), int(kv['y']), int(kv['w']), int(kv['h']))
            except Exception:
                return None
    return None


# ---------------------------------------------------------------------
def push_one(board, src, dest, dry=False, quiet=False):
    """传单个文件 + 核 md5。返回 True/False"""
    name = os.path.basename(src)
    size = os.path.getsize(src)
    lm = md5_local(src)

    if dry:
        print("    [dry-run] scp %s -> %s:%s/   (%.1f KB, md5 %s)"
              % (name, board, dest, size / 1024.0, lm[:16]))
        return True

    p = subprocess.run(['scp', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        src, '%s:%s/' % (board, dest)],
                       capture_output=True, text=True, env=_scp_env(),
                       encoding='utf-8', errors='replace')
    if p.returncode != 0:
        print("    !! scp 失败：%s" % (p.stderr or p.stdout).strip())
        return False

    ok, rm = ssh(board, 'md5sum %s/%s 2>/dev/null | cut -d" " -f1'
                 % (dest.rstrip('/'), name))
    if not ok or not rm:
        print("    !! 传上去了但读不到 md5（%s）—— 请手动核" % rm)
        return False
    rm = rm.strip()
    if rm != lm:
        # 本地是 Windows，md5sum 读到的是二进制内容，若被当成文本处理会有差异；
        # 这里不一致就是真的不一致（scp 默认二进制传输）。
        print("    !! md5 不符：本地 %s  板上 %s" % (lm[:16], rm[:16]))
        return False
    if not quiet:
        print("    ✓ %s  (%.1f KB, md5 %s…)" % (name, size / 1024.0, lm[:16]))
    return True


def collect(paths, recursive):
    """展开成待传文件列表（目录按 -r 展开）"""
    out = []
    for p in paths:
        if os.path.isdir(p):
            if not recursive:
                print("!! %s 是目录 —— 加 -r 才会传（避免误传一堆东西）" % p)
                return None
            for r, _, fs in os.walk(p):
                for f in fs:
                    out.append(os.path.join(r, f))
        elif os.path.isfile(p):
            out.append(p)
        else:
            print("!! 找不到 %s" % p)
            return None
    return out


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description='把文件 / 图片推到 PYNQ 板（图片自动转 RGB565 + 配 golden）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('【用法】')[1].split('【它不是万能的】')[0])
    ap.add_argument('files', nargs='+', help='要推的文件（图片 / .bin / 任意文件）')
    ap.add_argument('--board', default=DEFAULT_BOARD,
                    help='板子地址（默认 %s）' % DEFAULT_BOARD)
    ap.add_argument('--dest', default=DEFAULT_DEST,
                    help='板上目标目录（默认 %s）' % DEFAULT_DEST)
    ap.add_argument('-r', '--recursive', action='store_true', help='目录递归')
    ap.add_argument('--roi', type=int, nargs=4, metavar=('X', 'Y', 'W', 'H'),
                    help='ROI；省略则用 auto_roi 自动估算')
    ap.add_argument('--no-golden', action='store_true',
                    help='图片不要算 golden（默认会算，方便板上对拍）')
    ap.add_argument('--keep', action='store_true',
                    help='保留中间产物（.bin / golden / 预览图）')
    ap.add_argument('--dry-run', action='store_true', help='只显示会做什么')
    ap.add_argument('-q', '--quiet', action='store_true')
    args = ap.parse_args()

    files = collect(args.files, args.recursive)
    if files is None:
        return 1

    print("=" * 68)
    print("  推向 %s:%s/" % (args.board, args.dest))
    print("  共 %d 个文件" % len(files))
    print("=" * 68)

    if not args.dry_run and not check_board(args.board):
        return 1
    if args.dry_run:
        print("  [dry-run] 跳过连通性检查")

    # 分离图片与其他
    tmpdir = None
    stage = []          # [(本地路径, 说明)]
    ok = True

    for f in files:
        if f.lower().endswith(IMG_EXT):
            if tmpdir is None:
                import tempfile
                tmpdir = tempfile.mkdtemp(prefix='push_')
            base = os.path.splitext(os.path.basename(f))[0]
            binp = os.path.join(tmpdir, base + '.bin')
            pngp = os.path.join(tmpdir, base + '_preview.png')
            print("\n  [图片] %s" % os.path.basename(f))
            rep, frac = crop_report(f)
            if rep:
                print("    %s" % rep)
                if frac > 0.25:
                    print("    ⚠⚠ 裁掉超过 1/4 —— 手掌若靠边很可能会被切掉。"
                          "建议先裁成 4:3 再传，或换一张")
            n, png = convert_image(f, binp, pngp)
            print("    → 转成 RGB565：%d 字节（期望 %d）" % (n, IN_BYTES))
            if n != IN_BYTES:
                print("    !! 字节数不对，跳过")
                ok = False
                continue
            stage.append((binp, '%s.bin（RGB565 输入帧）' % base))
            if png:
                print("    → 预览图（**转换后的真实画面**）：")
                print("      %s" % png)
                print("      ⚠ 打开看一眼 —— 上面那行说的「裁掉多少」在这里是可见的")

            if not args.no_golden:
                roi = tuple(args.roi) if args.roi else auto_roi_of(binp)
                if roi is None:
                    print("    !! auto_roi 估不出 ROI，golden 跳过（可用 --roi 指定）")
                else:
                    gp = os.path.join(tmpdir, base + '_golden.bin')
                    groi = roi if args.roi else (roi[0], roi[1],
                                                 roi[2], roi[3])
                    good, err = make_golden(binp, gp, groi)
                    if good:
                        stage.append((gp, '%s_golden.bin（ROI=%s）'
                                      % (base, ' '.join(map(str, groi)))))
                        print("    → golden 已算，ROI=%s"
                              % ' '.join(map(str, groi)))
                    else:
                        print("    !! golden 失败：%s" % err)
                        ok = False
            # .bin 自己不传（板子上要的是转换后的那个）
            continue
        stage.append((f, ''))

    if not stage:
        print("\n没有可传的东西。")
        return 1

    print("\n--- 传输 ---")
    for path, note in stage:
        print("  %s%s" % (os.path.basename(path),
                          ('   ← ' + note) if note else ''))
        if not push_one(args.board, path, args.dest, dry=args.dry_run,
                        quiet=args.quiet):
            ok = False
            break

    # 清 __pycache__（脚本改了但板上是旧字节码，是踩过的坑）
    if not args.dry_run and ok:
        ssh(args.board, 'rm -rf %s/__pycache__ 2>/dev/null || true'
            % args.dest.rstrip('/'), check=False)

    if tmpdir and not args.keep:
        shutil.rmtree(tmpdir, ignore_errors=True)
    elif tmpdir:
        print("\n中间产物保留在：%s" % tmpdir)

    print()
    if ok:
        print("=" * 68)
        print("  完成。板上验证：")
        print("    import os,sys; os.chdir('%s'); sys.path.insert(0,'%s')"
              % (args.dest, args.dest))
        print("    from gesture_overlay import GesturePipeline")
        print("    g = GesturePipeline(); g.print_info(); g.setup_dma()")
        print("    g.config(roi_x=.., roi_y=.., roi_w=.., roi_h=..)")
        print("    g.in_buf[:] = np.fromfile('%s/<名字>.bin', dtype=np.uint8)"
              % args.dest)
        print("    g.in_buf.flush(); g.run_once(); g.get_result()")
        print()
        print("  ⚠ ROI 三处必须一致：生成 golden 的 / 板上 config() 的 / 原图取景")
        print("=" * 68)
    else:
        print("!! 有失败项，见上。")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
