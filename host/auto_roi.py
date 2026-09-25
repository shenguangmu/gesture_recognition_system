#!/usr/bin/env python3
"""
auto_roi.py —— 自动跟随手部的 ROI 估算（方案 B：尺寸恒定，中心跟手）

【为什么需要它】

`gesture_preproc` 的 ROI 是**四个常量寄存器**（roi_x/roi_y/roi_w/roi_h），
由 PS 写死 —— **PL 完全不知道手在哪**。后果（2026-09-23 实测）：

    ROI=(160,80) 320x320  →  拇指被切出框外，输出只剩四根手指
    ROI=(96,32) 448x448   →  五指齐全

也就是说：**手一动，特征就丢**。本模块在启动 PL 之前，
先从 DDR 里的整帧（640x480 RGB565）估算出"手在哪"，再写 ROI 寄存器。

    DDR(VDMA 帧缓存, 640x480 RGB565)
         │
         ├──► auto_roi.py  → 算出 roi_x/roi_y  ──┐
         │                                        │
         └────────────────────────────────────────┴──► 写寄存器 → ap_start
                                                         gesture_preproc

【⚠ 的顺序要求】
  必须 **先算完 ROI → 再写参数 → 最后 ap_start**。
  反了会用到上一帧的 ROI。驱动里 `preproc_driver.c` 本来就是
  "先写参数后启动"，但调用方要保证 compute 在 config 之前。

================================================================================
  语义：方案 B（尺寸恒定 448x448，中心跟手）
================================================================================

  ┌──────────┐  算出前景包围盒 bbox
  │  手 bbox  │  bbox 中心 = (cx, cy)
  └──────────┘  ROI 中心 = bbox 中心，**尺寸恒为 448x448**
                ROI 左上角 = (cx-224, cy-224)，再钳到 [0, 640-448] x [0, 480-448]

  ⭐⭐ **为什么 ROI 必须是正方形**（2026-09-25 查清，原先给的理由不对）

  因为 `crop_scale` 对 x / y 是**独立缩放**的
  （见 `host/gesture_golden.py`，HLS 侧同公式）：

      xs = (arange(97) * roi_w) // 96     # 用 roi_w
      ys = (arange(97) * roi_h) // 96     # 用 roi_h
      → 输出恒为 96x96

  于是手的输出宽高比 = (手_w / 手_h) × (roi_h / roi_w)。
  **roi_w != roi_h 会把手的形状拉伸变形** —— 而手形正是 CNN 要学的东西。

  ⚠ 这里原先写的是「尺寸恒定 → 缩放比恒定 → gain 标定一直有效」，
    把**形状失真**这个真正的理由漏了，却把 gain 当成了主要论据。
    2026-09-25 实测证伪了那个论据（见文件尾附录 A）。
    **结论（正方形）不变，理由换掉。**

  **为什么尺寸恒定，而不是"bbox + margin 外扩"（方案 A）**：
    · 尺寸恒定 → 手在 96x96 里的**表观尺寸分布稳定**。这是关键 ——
      方案 A 手一张一缩，同一类手势的输入分布发散，CNN 更难学。
    · 代价：手很小时框里有较多背景。可接受 —— CNN 看的是手形，不是占比。
    · ⚠ **不是**因为 gain。gain 是 Q8 增益（256 = ×1.0），补的是
      "在 96x96 上算 Sobel 梯度偏弱"（见 `gesture_preproc.cpp:15`）。
      实测改 gain 到 128..512 对二值输出只有 0~4.5% 差异 ——
      因为 `thresh_mode=1` 下前景像素本来就全部饱和在 255。

  **为什么钳位一定有解**：只要 size <= 640 且 size <= 480，
  所以 [0, W-size] 恒非空 —— 但**这不保证装得下手**，见下。

  ⭐⭐ **为什么默认 448，而不是 320**（实测改的，别改回去）

  一开始按"赛题配置 320x320"把默认设成 320，**实测发现会切掉手**：

      图       手 bbox 尺寸    固定 320 的覆盖率
      ─────────────────────────────────────────
      力手      241x281         100.0%  （装得下）
      合成图    309x391          **81.8%**  ← 上下各切掉 ~35 px

  合成图的手**比 320 还高**（391 > 320）—— 固定 320 在数学上就装不下。
  各种尺寸实测：

      size=320 : 力手 100.0% | 合成  81.8%   ← 会切手
      size=384 : 力手 100.0% | 合成  98.2%
      size=448 : 力手 100.0% | 合成 100.0%   ← 取这个
      size=480 : 力手 100.0% | 合成 100.0%

  ⚠ 我最初的推理错在：只验了"320 <= 画面 480"（**画面边界**），
    忘了"手本身可能比 320 大"。钳位保证的是 ROI 在画面内，
    **不保证** ROI 装得下前景 —— 这是两回事。
    `compute_roi()` 现在会对"装不下"报警（`fits` 字段）。

  **代价**：448 时手只占 ROI 的 34%~60%，周边是背景。
  缩放后 96x96 里手偏小 —— 但**手上特征完整**。
  宁可要"完整的小手"，不要"残缺的大手"：
  CNN 学的是手形，缺一根手指比手小一圈严重得多。

  ⭐ 用 448 还有一个附带好处：它与人工为力手调出的最优配置
    `(96,32,448,448)` **尺寸一致**，所以那次实测的 `gain=256`
    对 448 同样适用（缩放比相同）。

  ⚠⚠ **但 448 现在也快不够了** —— 2026-09-25 新增一张实测图
    （OK 手势 + 小臂伸入画面），bbox 高度 **460**，448 已经装不下
    （上下各切 6 px）。见文件尾附录 B。

================================================================================
  ⚠⚠ 已知缺陷：bbox 会把**小臂**算进来（2026-09-25 实测发现）
================================================================================

  判据是"与背景亮度差 > 25"，**不分手和臂** —— 手腕、小臂与手在亮度上
  连通，于是 bbox 一路拖到画面边界。

  实测（`ok_hand.bin`，OK 手势 + 小臂从右下伸入）：

      bbox = (134, 20, 483, 479)   350x460
                                   ↑ 下沿 479 = 画面底边

  后果有两个，都严重：

    1. **bbox 高度虚高** —— 460 里相当一部分是小臂，不是手
       → 于是"手装不下"的告警其实是被小臂触发的
    2. **中心被拉低** —— cx/cy 是 bbox 中心，小臂把手往下拽
       → ROI 定位整体偏移，手在框里偏上

  ⚠ 这不是新图特有的：力手和合成图的小臂也在画面里，
    只是没连到边界、没暴露得这么明显。

  **试过的解法与结论**：

    · 形态学腐蚀断开细颈 → **没用**。小臂和手掌是**宽连接**，
      不是细颈；能断开小臂的腐蚀强度会把手掌也一起吃掉。
    · 按前景像素数反推真实尺寸 → **没用**。OK 手势的圈是空心的，
      mask 面积 ≠ 外接尺寸。

  **可行的方向**（都**未实现、未验证**，留待上板用连续帧测）：

    · **最小外接矩形**而不是轴对齐 bbox —— OK 手势的手指是斜的，
      轴对齐框虚高；但旋转框会让 ROI 也跟着旋转，而 `crop_scale`
      只做轴对齐裁剪 → 需要额外的旋转步骤，代价大
    · **连通域分析取最大连通域**，再配"按行宽变化"判断手腕位置 ——
      需要 scipy（**板上已确认有 scipy 1.8.0**，本机 PC 没有）
    · **构图约束**：要求手在画面中上部、小臂不进框 —— 最省事，
      但要写进操作说明

  ⚠ 现阶段**不要**依赖 auto_roi 的 bbox 做精确尺寸判断，
    把它当"手大致在哪"的粗估即可；`fits` 告警要人工看一眼 cause。

================================================================================
  检测算法：**对亮度中位数的绝对偏离**（不是"亮于阈值"）
================================================================================

  判据：  前景 = |Y - background_Y| > thresh        (默认 thresh=25)

  ⭐ 为什么用**绝对偏离**而不是"Y > 阈值"：
     两张测试图的手/背景**亮度关系相反** ——

        力手（白墙背景）  背景 Y≈48    手 Y≈196   手**更亮**
        合成图（浅灰背景）背景 Y≈224   手 Y≈124   手**更暗**

     固定极性的阈值不可能同时处理两者。取**绝对偏离**后，
     "比背景亮"和"比背景暗"都算前景，极性自动兼容。

  ⭐ 为什么 background_Y 取**画面边框的中位数**，不取全图中位数：
     全图中位数隐含假设"背景占画面 >50%"。手凑近镜头时手会占满画面，
     这个假设就崩了（中位数会变成手的亮度 → 遮罩全空 → 回退居中，
     跟随失效）。画面**最外圈**几乎必然是背景，是更可靠的估计。

     实测两图：边框中位数 vs 全图中位数结果**完全一致**（都命中），
     但边框法在"手占满画面"时不会崩。故默认用边框法。

     ⚠ 已知前提：**画面边缘必须是背景**（摄像头不能贴着手拍）。
     若边缘有大量前景，可调大 --border 或改用全图中位数。

  ⭐ 实测的**参数不敏感度**（力手图，这是好事，说明不用精调）：
     · thresh 扫 15/20/25/30/35/40 → bbox 逐像素相同
     · border 扫 0.04/0.08/0.12/0.20 → bg 恒为 32.0，ROI 相同

================================================================================
  ⚠ 方案 B 的固有限制：**贴着画面边缘时跟不动**
================================================================================

  实测（把力手图整体平移）：

      平移(   0,   0) → ROI x= 96 y= 32
      平移(-100,   0) → ROI x=  0 y= 32     ← 已撞左边界
      平移( +80, -60) → ROI x=176 y=  0     ← 已撞上边界
      平移(+120, +60) → ROI x=192 y= 32

  原因：448 的 ROI 放在 640x480 画面里，
        x 方向活动余量只有 640-448 = **192 px**
        y 方向活动余量只有 480-448 = **32 px**   ← 极小！
  所以手一旦靠上/下边，ROI 就钳在边界上，跟随失效 ——
  **手会重新掉出框外**，只是程度比固定 ROI 轻。

  ⭐⭐ **量化（2026-09-25 补）** —— 这才是问题的真正尺度：

      正方形边长 s   x 余量    y 余量
      ─────────────────────────────────
          288          352      192
          320          320      160
          352          288      128
          384          256       96
          416          224       64
          448          192       32     ← 现状
          480          160        0

    ⚠ 而且**这不是选错尺寸造成的** —— 是构图的必然结果：
      合成图的手高 391，占画面 480 的 **81%**。
      要装下它，正方形 ROI 至少 400 → y 余量最多 80 px。
      **手只要占画面 8 成，活动余量就只能是几十像素量级。**

    ⇒ 真正的杠杆是**构图**（让手占画面 ~50% 而不是 81%），
      不是 ROI 算法：

          手占画面   手像素高   装下它需   480 里的 y 余量
          ──────────────────────────────────────────────
            81%       391        400           80
            60%       288        320          160
            50%       240        256          224

  ⚠ 这是**方案 B 的固有代价**，不是实现 bug。取舍：

      尺寸大 → 装得下手（fits）但跟不动（活动余量小）
      尺寸小 → 跟得动但切手

  缓解手段（择一，都需要先上板实测再定）：
    · **改构图**（首选）—— 摄像头后退 / 换广角，让手占画面一半左右，
      再用固定 320。零代码、不破坏对拍、gain 标定已有实测背书
    · 手靠边时**动态缩小 ROI** —— 尺寸变了 → 手在 96x96 里的表观尺寸
      逐帧变化 → **CNN 必须做尺寸增强并重训**。但好处是"手占比恒定"
      （一种归一化），长期可能更稳
    · **分层尺寸**（如只在 {320,448} 两档间切换）—— 每档都有标定，
      CNN 训练时两档都喂。比连续动态简单得多
    · ⚠ **平滑跟随**治的是**抖动**，不是**活动范围** ——
      在 y 余量 32 px 这个量级下，平滑只会让跟随更迟滞。
      它值得做，但**不是这个问题的解**。

  ⚠ 现在能验的只有**单帧**。平移测试是合成的，真实手移动是否跟得住，
  要等 USB 摄像头到了用**连续帧**验（见文件尾「待验证」）。


================================================================================
  已知限制（照实记，别当成"完全自动"）
================================================================================

  1. **不是手部识别** —— 任何"与背景亮度差 > 25"的东西都算前景。
     臂膀、衣袖、桌上的深色物体都会进来。需要靠构图约束（手在中间）。
  2. **背景变亮/变暗时**：边框估计会自动跟上，无需改参数。
     但**渐变背景**（如灯光渐变）会让边框估计本身不可靠。
  3. **⚠ 会破坏逐字节对拍**：ROI 逐帧变化后，无法预先算 golden。
     调试期靠对拍建立的信心的参照会失去 ——
     改用 `--draw` 输出的轨迹图验证"跟得住"。见 docs/board-bringup-guide.md。
  4. 阈值化 + 包围盒是**逐像素扫描**，640x480 在 A9 上约几 ms~几十 ms。
     而 `gesture_preproc` 一帧只要 6 ms（板测），CNN 也不是 30fps 实时，
     所以 PS 侧跑来得及 —— **但要在目标板上实测确认**，别拿 PC 数字外推。

【用法】

  # 看一帧的估算结果 + 画出来（PC 上就能跑，不需要板子）
  python host/auto_roi.py --input samples/frame_640x480_rgb565.bin --draw roi.png

  # 直接打印板上要用的 config 行
  python host/auto_roi.py --input frame.bin --print-config
      → g.config(roi_x=96, roi_y=32, roi_w=448, roi_h=448, ...)

================================================================================
  ✅ 已验证（2026-09-24，PC 侧，单帧）
================================================================================

  1. **复现人工最优配置** ← 最有说服力的一条
     力手图上自动给出 `ROI=(96,32,448,448)`，
     与 `docs/board-test-log-2026-09-23.md` §9.3 人工反复实测调出的
     "✅ 五指齐全、指缝完整"配置**完全一致**。
  2. 合成图（手比背景**暗**，极性相反）同样正确：
     bbox=(162,65,470,455)，ROI=(92,32,448,448)，fits=True
     → 证明"绝对偏离"判据确实兼容两种极性。
  3. 参数不敏感：thresh 15..40、border 0.04..0.20 都给同一结果。
  4. 回退路径：全黑画面 → fallback=True，ROI 回到居中 (96,16)。✅
  5. 装不下会报警：320 时合成图 fits=False, cut=(0,35,0,36)。✅

================================================================================
  ⚠ 待验证（USB 摄像头到货后，约 2026-09-26）
================================================================================

  - **连续帧跟随**：现在是逐帧独立算的，没有平滑。
    真实手移动时 ROI 会不会抖？抖动会不会让 CNN 输入不稳定？
  - **A9 上的耗时**：PC 上几 ms，板上要实测。
    若 > 20 ms 就要考虑降采样后再统计（每 2 像素取 1 个）。
  - **贴边跟不动**：见文件头「方案 B 的固有限制」。
    用真实构图确认"手放中部"是否够用，不够就得做动态尺寸。
  - **背景渐变/阴影**：现有两图背景都均匀。真实场景（灯光、阴影）
    下边框估计是否仍可靠，**未验**。
  - ⚠ **不能用逐字节对拍验证**（ROI 逐帧变，无法预先算 golden）。
    替代手段是 `--draw` 的轨迹图 + 目视确认"手始终在框内"。

================================================================================
  附录 A：ROI 尺寸到底影响什么（2026-09-25 参数扫描）
================================================================================

  用 `host/gesture_golden.py`（与硬件逐位一致）扫的参数，力手帧：

  **① gain 几乎无影响** —— 这一条推翻了原先"尺寸恒定是为了保住 gain"的论据：

      gain     与 gain=256 的输出差异
      ───────────────────────────────
      128        4 / 9216  (0.0%)
      192        4 / 9216  (0.0%)
      384       50 / 9216  (0.5%)
      512      411 / 9216  (4.5%)

      ⚠ gain 是 **Q8 增益**（256 = ×1.0），补的是"在 96x96 上算 Sobel
        梯度偏弱"（见 `src_hls/gesture_preproc.cpp:15`）。
        但 `thresh_mode=1` 下前景像素**本来就全部饱和在 255**
        （实测：ROI 448 和 320 下，100% 的前景像素都等于 255），
        所以 gain 加倍也改变不了二值结果。

  **② 真正的影响是"手的表观尺寸"** —— 同一只手、同一中心 (320,260)：

      ROI 边长   非零/9216    vs 448 差异
      ────────────────────────────────────
        448        1992          —
        384        2287       25.5%
        320        2848       32.1%
        480        1694       11.2%

      ⇒ 尺寸一变，输入分布就变 —— **CNN 得重训**。
        这才是"尺寸要恒定"的真正理由，而且它依然成立。

================================================================================
  附录 B：新实测图暴露的问题（2026-09-25）
================================================================================

  新增一张实测图：OK 手势 + 小臂从右下伸入画面。

  **① bbox 把小臂算进来了**

      bbox = (134, 20, 483, 479)      350 x 460
                                      ↑ 下沿 479 = 画面底边

      判据"与背景亮度差 > 25"**不分手和臂**，小臂一路把框拖到画面底。
      后果：bbox 高度虚高（460 里相当部分是小臂）、中心被拉低。

      这也解释了为什么 448 会告警"装不下" —— 是被**小臂**触发的。
      详见文件头「已知缺陷：bbox 会把小臂算进来」。

  **② 但主要特征没丢** —— 各 ROI 下 OK 的圈（拇指-食指）都保住了：

      方案                        ROI              非零/9216
      ────────────────────────────────────────────────────
      auto_roi 建议（448）        (84,26)448x448     3021
      ROI 480 顶格                (68,0)480x480      2893
      人工只框手指                (110,5)400x400     3142
      人工只框手指                (130,10)360x360    3314

      ⇒ 缩小 ROI 把手臂排除掉，反而**前景占比更高**（33%→36% 非零）。
        对 CNN 来说"只手、无臂"其实是更干净的输入。

  **③ 待定**：是否应该主动**排除小臂**（而不是让它进框）？
      需要用连续帧确认排除后手指特征是否始终完整 —— 单帧说明不了。

================================================================================
  ⚠ 本节结论全部来自 **PC 侧 golden**（与硬件逐位一致，但不是硬件）。
     上板复核待 USB 摄像头到货后用连续帧做。
================================================================================
"""

import argparse
import io
import sys

# ⚠ Jupyter 兼容：IPython 的 sys.stdout 是 OutStream，没有 .buffer。
#    必须有守卫，否则 `%run auto_roi.py` 会 AttributeError 崩掉。
#    （与 capture_frame.py / usb_camera_run.py 同款处理。）
if hasattr(getattr(sys.stdout, 'buffer', None), 'write') \
   and getattr(sys.stdout, 'encoding', '').lower() not in ('utf-8', 'utf8'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import numpy as np

# ⚠ 与 PL 侧保持一致的常量（改这里要同步 src_hls/gesture_preproc.h）
IN_WIDTH  = 640
IN_HEIGHT = 480
OUT_SIZE  = 96          # CNN 契约，本模块不用，但提示 ROI 必须 >= 它

# 算法默认参数（实测不敏感，见文件头）
# ⚠ ROI_SIZE 默认 448 而不是 320 —— 这是**实测改的**，理由见文件头
#   「为什么默认 448」。320 会切掉"合成手势图"的上下各 ~35 px。
DEFAULT_ROI_SIZE   = 448   # 方案 B：ROI 边长恒定
DEFAULT_THRESH     = 25    # |Y - bg| > 25 判为前景
DEFAULT_BORDER     = 0.08  # 边框宽度占比，用于估背景
DEFAULT_MIN_FG     = 0.02  # 前景占比低于此值 → 认为检测失败，回退居中

# BT.601 整数权重，与 src_hls/gesture_preproc.cpp 的 RGB565→灰度一致
Y_R, Y_G, Y_B = 66, 129, 25


def rgb565_to_gray(rgb565: np.ndarray) -> np.ndarray:
    """(H,W) uint16 RGB565 → (H,W) uint8 灰度。

    ⚠ 必须与 PL 侧逐位一致（src_hls/gesture_preproc.cpp 的 crop_scale）：
      位序 R[15:11] G[10:5] B[4:0]；
      r/g/b 分别**左移补齐**到 8 位（r<<3 / g<<2 / b<<3，**不是拉伸**）；
      lum = (r*66 + g*129 + b*25) >> 8。

    ⚠ 左移补齐（而非线性拉伸到 255）会让灰度整体偏暗 —— 这是 PL 的既有
      行为，参考实现 gesture_ref.cpp / gesture_golden.py 全都照抄了它，
      所以**这里也必须照抄**，否则估出来的 ROI 会和 PL 看到的图不一致。
    """
    r8 = (((rgb565 >> 11) & 0x1F).astype(np.uint32) << 3)
    g8 = (((rgb565 >> 5) & 0x3F).astype(np.uint32) << 2)
    b8 = ((rgb565 & 0x1F).astype(np.uint32) << 3)
    lum = r8 * Y_R + g8 * Y_G + b8 * Y_B
    return (lum >> 8).astype(np.uint8)


def estimate_background(gray: np.ndarray, border_frac: float = DEFAULT_BORDER) -> float:
    """用画面最外圈像素的中位数估计背景亮度。

    比全图中位数稳：手凑近镜头占满画面时，全图中位数会被手带偏，
    而边框几乎必然是背景。见文件头「为什么取边框」。
    """
    h, w = gray.shape
    by = max(1, int(h * border_frac))
    bx = max(1, int(w * border_frac))
    m = np.zeros_like(gray, dtype=bool)
    m[:by, :] = True
    m[-by:, :] = True
    m[:, :bx] = True
    m[:, -bx:] = True
    return float(np.median(gray[m]))


def foreground_mask(gray: np.ndarray, bg: float,
                    thresh: float = DEFAULT_THRESH) -> np.ndarray:
    """前景遮罩 = |Y - bg| > thresh。

    ⚠ 是**绝对偏离**，不是 "Y > 阈值" —— 手比背景亮或暗都算前景。
      见文件头「为什么用绝对偏离」。
    """
    return np.abs(gray.astype(np.int32) - int(round(bg))) > thresh


def bbox_of(mask: np.ndarray):
    """遮罩的包围盒 → (x0, y0, x1, y1) 闭区间；全空返回 None。"""
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def clamp_roi(cx: float, cy: float, size: int,
              width: int = IN_WIDTH, height: int = IN_HEIGHT):
    """以 (cx,cy) 为中心、边长 size 的 ROI，钳到画面内。

    ⚠ 钳位一定有解：size <= width 且 size <= height（顶层会挡，见下）。
    """
    if size > width or size > height:
        raise ValueError("ROI 边长 %d 超过画面 %dx%d" % (size, width, height))
    x0 = int(round(cx - size / 2.0))
    y0 = int(round(cy - size / 2.0))
    x0 = max(0, min(x0, width - size))
    y0 = max(0, min(y0, height - size))
    return x0, y0


def compute_roi(rgb565: np.ndarray,
                size: int = DEFAULT_ROI_SIZE,
                thresh: float = DEFAULT_THRESH,
                border_frac: float = DEFAULT_BORDER,
                min_fg: float = DEFAULT_MIN_FG,
                width: int = IN_WIDTH,
                height: int = IN_HEIGHT) -> dict:
    """一帧 → ROI 参数。返回 dict，含结果与**诊断信息**。

    ⚠ 返回诊断字段（bg / fg_frac / bbox / fallback）是刻意的：
      这个模块的失败模式是"静默回退居中"，不告诉你就等于没检测。
      调用方**应当**把 fallback=True 记进日志。

    回退条件（任一）：
      · 前景占比 < min_fg            → 画面里没有手 / 阈值太严
      · 包围盒退化（宽或高 <= 2 px） → 噪声点，不是手
    """
    if rgb565.shape != (height, width):
        raise ValueError("输入形状 %s 与 %dx%d 不符" % (rgb565.shape, width, height))

    gray = rgb565_to_gray(rgb565)
    bg = estimate_background(gray, border_frac)
    mask = foreground_mask(gray, bg, thresh)
    fg_frac = float(mask.mean())
    bb = bbox_of(mask)

    degenerate = (bb is None) or (bb[2] - bb[0] <= 2) or (bb[3] - bb[1] <= 2)
    fallback = (fg_frac < min_fg) or degenerate

    if fallback:
        cx, cy = width / 2.0, height / 2.0
        why = ("前景占比 %.3f%% < %.1f%%" % (fg_frac * 100, min_fg * 100)) \
              if fg_frac < min_fg else "包围盒退化"
    else:
        cx = (bb[0] + bb[2]) / 2.0
        cy = (bb[1] + bb[3]) / 2.0
        why = ""

    x0, y0 = clamp_roi(cx, cy, size, width, height)

    # ⚠ "ROI 在画面内" 和 "ROI 装得下手" 是**两回事**。
    #   钳位保证前者；这里检查后者 —— 装不下时必须能看出来，
    #   否则就是本项目最忌讳的静默失败（ROI 看着正常，手被切掉一截）。
    if bb is None:
        fits, cut = True, (0, 0, 0, 0)
    else:
        x0b, y0b, x1b, y1b = bb
        x1r, y1r = x0 + size - 1, y0 + size - 1
        cut = (max(0, x0 - x0b), max(0, y0 - y0b),      # 左、上被切掉多少
               max(0, x1b - x1r), max(0, y1b - y1r))    # 右、下被切掉多少
        fits = (cut == (0, 0, 0, 0))

    return {
        'roi_x': x0, 'roi_y': y0, 'roi_w': size, 'roi_h': size,
        'bg': bg, 'thresh': thresh, 'fg_frac': fg_frac,
        'bbox': bb, 'center': (cx, cy), 'fallback': fallback, 'reason': why,
        'fits': fits, 'cut': cut,
        'mask': mask,
    }


def draw_overlay(rgb888: np.ndarray, res: dict, out_path: str) -> None:
    """把检测结果画在图上：遮罩轮廓 + 包围盒(黄) + 最终 ROI(绿)。

    用途是**肉眼验证"跟得住"** —— ROI 动态化后无法逐字节对拍，
    轨迹可视化就是替代的验证手段。
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from PIL import Image

    fig, ax = plt.subplots(1, 2, figsize=(14, 5.5))
    ax[0].imshow(rgb888); ax[0].set_title('input')
    ax[1].imshow(res['mask'], cmap='gray')
    ax[1].set_title('mask: |Y - bg(%.0f)| > %d   fg=%.2f%%'
                    % (res['bg'], res['thresh'], res['fg_frac'] * 100))

    for a in ax:
        if res['bbox'] is not None:
            x0, y0, x1, y1 = res['bbox']
            a.add_patch(plt.Rectangle((x0, y0), x1 - x0, y1 - y0,
                                      fill=False, edgecolor='yellow', lw=1.5,
                                      label='foreground bbox'))
        c = 'red' if res['fallback'] else 'lime'
        ttl = 'ROI (%d,%d) %dx%d%s' % (res['roi_x'], res['roi_y'],
                                       res['roi_w'], res['roi_h'],
                                       '  [FALLBACK: %s]' % res['reason'] if res['fallback'] else '')
        a.add_patch(plt.Rectangle((res['roi_x'], res['roi_y']),
                                  res['roi_w'], res['roi_h'],
                                  fill=False, edgecolor=c, lw=2.5, label=ttl))
        a.plot(res['center'][0], res['center'][1], 'r+', ms=14, mew=2)
        a.legend(loc='upper right', fontsize=7)
    plt.tight_layout()
    plt.savefig(out_path, dpi=80)
    plt.close(fig)
    print("[画图] %s  ← 黄=前景包围盒  绿/红=最终 ROI  红=回退居中" % out_path)


def load_frame(path: str, width: int = IN_WIDTH, height: int = IN_HEIGHT) -> np.ndarray:
    """读裸 .bin（uint16 小端，行优先）→ (H,W) uint16。"""
    d = np.fromfile(path, dtype='<u2')
    if d.size != width * height:
        raise SystemExit("字节数不对：%d 像素，期望 %d（%dx%d）"
                         % (d.size, width * height, width, height))
    return d.reshape(height, width)


def main() -> int:
    ap = argparse.ArgumentParser(
        description='自动估算跟随手部的 ROI（方案 B：尺寸恒定，中心跟手）')
    ap.add_argument('--input', required=True, help='640x480 RGB565 裸 .bin')
    ap.add_argument('--width', type=int, default=IN_WIDTH)
    ap.add_argument('--height', type=int, default=IN_HEIGHT)
    ap.add_argument('--size', type=int, default=DEFAULT_ROI_SIZE,
                    help='ROI 边长，恒为正方形（默认 %d；'
                         '⚠ 改小会把超尺寸的手切掉，见文件头「为什么默认 448」）'
                         % DEFAULT_ROI_SIZE)
    ap.add_argument('--thresh', type=float, default=DEFAULT_THRESH,
                    help='前景判据 |Y-bg| 的阈值（默认 %d，实测 15..40 不敏感）'
                         % DEFAULT_THRESH)
    ap.add_argument('--border', type=float, default=DEFAULT_BORDER,
                    help='估背景用的边框宽度占比（默认 0.08）')
    ap.add_argument('--min-fg', type=float, default=DEFAULT_MIN_FG,
                    help='前景占比低于此值则回退居中（默认 0.02）')
    ap.add_argument('--draw', metavar='PNG', help='把结果画出来存成 PNG')
    ap.add_argument('--print-config', action='store_true',
                    help='打印板上可直接粘贴的 g.config(...) 调用')
    args = ap.parse_args()

    frame = load_frame(args.input, args.width, args.height)
    res = compute_roi(frame, size=args.size, thresh=args.thresh,
                      border_frac=args.border, min_fg=args.min_fg,
                      width=args.width, height=args.height)

    print("=" * 66)
    print("  自动 ROI 估算 —— 方案 B（尺寸恒定 %dx%d，中心跟手）" % (args.size, args.size))
    print("=" * 66)
    print("  背景亮度估计 bg   : %.1f  （边框 %d%% 像素的中位数）"
          % (res['bg'], int(args.border * 100)))
    print("  前景判据          : |Y - bg| > %d" % res['thresh'])
    print("  前景占比          : %.2f%%" % (res['fg_frac'] * 100))
    print("  前景包围盒        : %s" % (str(res['bbox']) if res['bbox'] else "无前景"))
    print("  包围盒中心        : (%.1f, %.1f)" % res['center'])
    print("  ──────────────────────────────────────────────────────")
    if res['fallback']:
        print("  [!!] 回退居中：%s" % res['reason'])
        print("       检查：画面里有没有手 / 背景是不是渐变 / --thresh 是否合适")
    if not res['fits'] and res['bbox'] is not None:
        c = res['cut']
        print("  [!!] ⚠ ROI 装不下前景！左右上下各切掉 %d/%d/%d/%d 像素"
              % (c[0], c[1], c[2], c[3]))
        print("       手高/宽 = %d/%d（含小臂？）"
              % (res['bbox'][3] - res['bbox'][1] + 1,
                 res['bbox'][2] - res['bbox'][0] + 1))
        print("       两种可能，处置方向相反 —— 先看 --draw 的图确认是哪种：")
        print("       (a) 手真的比 %d 大 → 调大 --size" % args.size)
        print("       (b) bbox 把**小臂**算进来了（下沿贴画面边就是它）")
        print("           → 这时调大 size 是**反的**，应该改构图让手居中、")
        print("             小臂不进框。见文件头「已知缺陷：bbox 会把小臂算进来」")
        print("       ⚠ 手被切掉一截时输出'看着正常'，是典型的静默失败 ——")
        print("         别忽略这条，它正是固定 ROI 那个老问题的翻版。")
    print("  ROI               : x=%d y=%d w=%d h=%d"
          % (res['roi_x'], res['roi_y'], res['roi_w'], res['roi_h']))
    print("=" * 66)

    if args.print_config:
        print()
        print("板上粘贴（⚠ 必须在 auto_roi 之后、ap_start 之前）:")
        print("  g.config(roi_x=%d, roi_y=%d, roi_w=%d, roi_h=%d,"
              % (res['roi_x'], res['roi_y'], res['roi_w'], res['roi_h']))
        print("           thresh_mode=1, thresh_offset=-8, gain=256,")
        print("           gauss_en=1, sobel_en=1, morph_en=1)")
        print()
        # ⚠ 让两个工具能对上：run.py 的默认 ROI 写在文件顶部，
        #   想让它俩一致就直接把下面这行贴进 run.py（只改数字，别改结构）。
        print("想让 run.py 的默认跟这个一致？改 host/run.py 顶部那一行：")
        print("  ROI = dict(x=%d, y=%d, w=%d, h=%d)"
              % (res['roi_x'], res['roi_y'], res['roi_w'], res['roi_h']))

    if args.draw:
        try:
            from PIL import Image
        except ImportError:
            print("(跳过画图：没装 Pillow)")
            return 0
        # 把 RGB565 还原成 888 便于目视（位左移补齐，与 PL 看到的一致）
        r = (((frame >> 11) & 0x1F).astype(np.uint8) << 3)
        g = (((frame >> 5) & 0x3F).astype(np.uint8) << 2)
        b = ((frame & 0x1F).astype(np.uint8) << 3)
        rgb888 = np.dstack([r, g, b])
        draw_overlay(rgb888, res, args.draw)

    return 0


if __name__ == '__main__':
    sys.exit(main())
