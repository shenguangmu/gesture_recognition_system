# ══════════════════════════════════════════════════════════════
#  cell —— 全幅 ROI(0,0,640x480) 跑合成手势图 + 与 golden 对拍
# ══════════════════════════════════════════════════════════════
import numpy as np
from gesture_overlay import GesturePipeline

ROI = (0, 0, 640, 480)

img = np.fromfile('/home/xilinx/frame.bin', dtype=np.uint8)
print('输入 frame.bin:', img.size, '字节')

g = GesturePipeline(bitfile='/home/xilinx/gesture_system.bit')
g.setup_dma()

g.config(roi_x=ROI[0], roi_y=ROI[1], roi_w=ROI[2], roi_h=ROI[3])
g.in_buf[:] = img
g.in_buf.flush()
g.run_once()
hw = np.asarray(g.out_buf, dtype=np.uint8).copy()
hw.tofile('/home/xilinx/hw_synth.bin')
print('  非零=%d/9216  范围=[%d,%d]  (golden 预期非零=1978)'
      % (int((hw != 0).sum()), int(hw.min()), int(hw.max())))

gd = np.fromfile('/home/xilinx/golden.bin', dtype=np.uint8)
d = np.flatnonzero(hw != gd)
print('\n=== 与 golden%s 对拍 ===' % (ROI,))
print('  %d/%d 不一致 → %.1f%% 一致' % (len(d), gd.size,
                                        100.0 * (gd.size - len(d)) / gd.size))

import matplotlib.pyplot as plt
fig, ax = plt.subplots(1, 2, figsize=(8, 4))
ax[0].imshow(hw.reshape(96, 96), cmap='gray', vmin=0, vmax=255)
ax[0].set_title('BOARD 96x96')
ax[1].imshow(gd.reshape(96, 96), cmap='gray', vmin=0, vmax=255)
ax[1].set_title('golden 96x96')
for a in ax: a.axis('off')
plt.tight_layout(); plt.show()
