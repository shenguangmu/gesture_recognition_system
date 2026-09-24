/**
 * @file    gesture_preproc.h
 * @brief   手势图像预处理链 —— 对外契约（寄存器映射 / 参数 / 共享类型）
 *
 * 本文件被三处共享，改之前先读 docs/architecture-contract.md：
 *   1. HLS 综合源码      src_hls/gesture_preproc.cpp
 *   2. HLS C 仿真 TB     src_hls/tb_gesture.cpp
 *   3. 各阶段参考实现    src_hls/gesture_ref.cpp
 *
 * ⚠ PS 侧驱动 **不** include 本文件，只同步常量/寄存器偏移。
 *   对应的文件是 sw/preproc_driver.h（曾误写作 sw/gesture_driver.h，
 *   该名字在本仓库从未存在过）。改这里的几何空间常量或寄存器偏移，
 *   要同步改的正是它。
 *
 * ────────────────────────────────────────────────────────────────────
 *  处理链（与实际实现一致）
 * ────────────────────────────────────────────────────────────────────
 *   RGB565 ─► crop_scale ─► gaussian ─► sobel ─► thresh
 *             640x480          └──── 以下全部在 96x96 上跑 ────┘
 *                                    │
 *                              morph ─► 96x96 灰度
 *
 * ⚠ 几何处理在最前：先裁剪缩放到 96x96，再做滤波/阈值/形态学。
 *   这是**省算力**的关键设计 —— 昂贵算子只在小图上跑（33 倍差距），
 *   与 gesture_preproc.cpp 文件头「处理链」一节一致。
 *   这里原先画成"先滤波后 roi_extract"，与实现相反，已更正。
 * ⚠ 各级的实际函数名见 .cpp 内部：crop_scale / gaussian_stage /
 *   sobel_stage / thresh_stage / morph_stage / output_stage
 *   （后五个是 static，只在本翻译单元内可见）。
 *
 * ────────────────────────────────────────────────────────────────────
 *  与 sobel_hls.h 的关系
 * ────────────────────────────────────────────────────────────────────
 *  sobel_accel 作为**独立 IP** 保留（BD 里仍可单独例化、单独回归），
 *  本文件里的链把它作为一个阶段重新实现了一份，两者数值行为一致。
 */

#ifndef GESTURE_PREPROC_H
#define GESTURE_PREPROC_H

#include <ap_int.h>
#include <hls_stream.h>
#include <ap_axi_sdata.h>

/* ================================================================== *
 *  一、图像规格
 * ================================================================== */

/** 采集分辨率。先按 VGA 640x480@30 走：
 *  RGB565 下 PCLK ≈ 24 MHz，自制转接板稳妥。
 *  改 720p 需重新评估 PCLK（RGB565 约 72 MHz）与转接板走线。 */
#define GESTURE_IN_WIDTH    640
#define GESTURE_IN_HEIGHT   480

/** 输入像素：RGB565，一拍一像素。
 *  DVP 侧"一行 2 拍"的拼装已经在 Verilog 采集模块里做完，
 *  到这里已经是规整的 16bit/像素流。 */
typedef ap_uint<16> rgb565_t;

/** 中间与输出像素 */
typedef ap_uint<8> gray_t;

/** 给 CNN 的输出尺寸（契约冻结，见 docs/architecture-contract.md §3.1）。
 *  ⚠ 改动这里必须同步通知 CNN 侧。 */
#define GESTURE_OUT_SIZE    96
#define GESTURE_OUT_PIXELS  (GESTURE_OUT_SIZE * GESTURE_OUT_SIZE)

/** 行缓存支持的图像上限 */
#define GESTURE_MAX_WIDTH   1920
#define GESTURE_MAX_HEIGHT  1080

/* ================================================================== *
 *  二、AXI4-Stream 传输类型
 * ================================================================== */

/** 输入流：16bit RGB565。
 *
 *  为什么用 ap_axiu 而不是裸 hls::stream<ap_uint<16>>：
 *  裸类型不生成 TLAST，AXI DMA 的 S2MM 靠 TLAST 界定一次传输的结束，
 *  缺了它 Vivado 会报
 *    "Interface connected to S_AXIS_S2MM does not have TLAST port"
 *  且 DMA 会一直等下去。这个坑现有 sobel 工程已经踩过，
 *  详见 legacy/sobel/README.md §9.6。 */
typedef ap_axiu<16, 0, 0, 0> axis_rgb_t;

/** 输出流：8bit 灰度 */
typedef ap_axiu<8, 0, 0, 0> axis_gray_t;

/* ================================================================== *
 *  三、AXI-Lite 寄存器映射
 *
 *  偏移由 Vitis HLS 的 INTERFACE s_axilite 自动生成，下面的宏是
 *  给驱动和 TB 用的可读对照表。
 *  导出 IP 后请用 drivers/ 下的 _hw.h 或 xparameters.h 复核。
 * ================================================================== */

#define GESTURE_REG_CTRL          0x00  /* bit0=ap_start, bit1=auto_restart */
#define GESTURE_REG_STATUS        0x04  /* bit0=ready,1=done,2=idle,3=continue */
#define GESTURE_REG_WIDTH         0x10  /* 输入宽  (默认 GESTURE_IN_WIDTH)  */
#define GESTURE_REG_HEIGHT        0x18  /* 输入高  (默认 GESTURE_IN_HEIGHT) */
#define GESTURE_REG_THRESH_MODE   0x20  /* 0=不用阈值(灰度直通), 1=自适应阈值 */
#define GESTURE_REG_THRESH_OFFSET 0x28  /* 自适应阈值偏置，有符号，默认 0   */
#define GESTURE_REG_GAUSS_EN      0x30  /* 0=跳过高斯, 1=高斯去噪(默认 1)   */
#define GESTURE_REG_SOBEL_EN      0x38  /* 0=直接阈值灰度, 1=先 Sobel(默认 1)*/
#define GESTURE_REG_MORPH_EN      0x40  /* 0=跳过闭运算, 1=闭运算(默认 1)   */
#define GESTURE_REG_GAIN          0x48  /* Sobel Q8 增益，256 = x1.0        */
#define GESTURE_REG_ROI_X         0x50  /* ROI 左上角 x                     */
#define GESTURE_REG_ROI_Y         0x58  /* ROI 左上角 y                     */
#define GESTURE_REG_ROI_W         0x60  /* ROI 宽（裁剪后缩放至 96x96）     */
#define GESTURE_REG_ROI_H         0x68  /* ROI 高                           */

/* CTRL 位 */
#define GESTURE_CTRL_AP_START      0x01u
#define GESTURE_CTRL_AUTO_RESTART  0x02u

/* STATUS 位 */
#define GESTURE_STATUS_AP_READY    0x01u
#define GESTURE_STATUS_AP_DONE     0x02u
#define GESTURE_STATUS_AP_IDLE     0x04u
#define GESTURE_STATUS_AP_CONTINUE 0x08u

/* ================================================================== *
 *  四、算法参数默认值
 * ================================================================== */

/** RGB565 → 灰度权重（BT.601 的整数近似，仿 Xilinx rgb2ycrcb）。
 *  取 8 位小数精度：0.2568->66, 0.5041->129, 0.0979->25，和 = 220。
 *  结果再右移 8 → 得 0..255。 */
#define GESTURE_Y_R   66
#define GESTURE_Y_G   129
#define GESTURE_Y_B   25

/** Sobel Q8 增益，256 = x1.0 */
#define GESTURE_DEFAULT_GAIN   256

/** 自适应阈值默认偏置（有符号 8 位，单位：灰度级）。
 *
 *  ⚠⚠ 阈值 = **全图均值** + offset —— 不是局部均值。
 *     （这里原写作"局部均值"，与实现不符，2026-09-24 更正。
 *      全仓库三处实现一致，是**注释**写错了，不是代码。）
 *
 *  offset 为负表示"低于均值也算前景"，
 *  正值表示"要比均值亮很多才算前景"。默认 −8。
 *
 *  ⚠ 关于"自适应"这个名字：本实现是**全图单均值归一**，
 *    粒度是"整幅 96×96 一个阈值"，**不是**分块/局部自适应。
 *    它能吸收整体光照强弱的变化，但**不能**补偿
 *    画面一半亮一半暗这类局部不均（阈值被全图拉平）。
 *    真正的分块自适应需要每块独立求均值（见 .cpp 里 thresh_stage 的注释）。
 *    对外描述本特性时请用"全图均值阈值"，别说"局部自适应阈值"。 */
#define GESTURE_DEFAULT_THRESH_OFFSET  (-8)

/** 形态学闭运算的核尺寸固定 3x3（结构元全 1） */
#define GESTURE_MORPH_K 1   /* 半径，3x3 = 1 两侧 */

/** ROI 默认值：居中取 320x320（后续缩放至 96x96） */
#define GESTURE_DEFAULT_ROI_W  320
#define GESTURE_DEFAULT_ROI_H  320

/* ================================================================== *
 *  五、各阶段函数：**不在本文件暴露**
 *
 *  ⚠ 这里原先声明了 6 个流式阶段函数
 *      rgb2gray / gaussian_3x3 / sobel_core /
 *      adaptive_thresh / morph_close / roi_extract
 *    但**全仓库从未有过它们的实现**（已核实：零调用点，仅声明）。
 *    留着会误导 —— 读到的人会以为可以单独例化/单独 csim，
 *    实际上链接就会报 undefined reference。已删除。
 *
 *  真正的各阶段实现是 src_hls/gesture_preproc.cpp 里的
 *      crop_scale / gaussian_stage / sobel_stage /
 *      thresh_stage / morph_stage / output_stage
 *  它们全部是 **static**（文件内可见），由顶层按 DATAFLOW 串联。
 *
 *  ⚠ 为什么不做成对外可调用的阶段函数：
 *    顶层必须是"纯流函数 + 参数按值传递"的结构（见 .cpp 里
 *    preproc_pipeline 的长注释）—— 把 s_axilite 参数直接用在
 *    DATAFLOW 区域内的函数里，会触发 [HLS 200-616] 仿真死锁。
 *    想单独跑某一级，正确做法是走 TB（src_hls/tb_gesture.cpp）。
 *
 *  ⚠ 名字撞车提醒：gesture_ref.cpp 里另有两个**同名但签名不同**的
 *    函数 adaptive_thresh / morph_close（参数是裸数组 + offset/enable），
 *    属于参考实现，与上面删掉的流式声明不是一回事，勿混。
 * ================================================================== */

/* ================================================================== *
 *  六、顶层：接口契约
 * ================================================================== */

/**
 * @brief 手势图像预处理顶层
 *
 * 接口约定（HLS INTERFACE 指令见 gesture_preproc.cpp）：
 *   src  -> AXI4-Stream (16bit RGB565 + TLAST)
 *   dst  -> AXI4-Stream (8bit  灰度  + TLAST)
 *   width/height/thresh_mode/thresh_offset/gauss_en/sobel_en/morph_en/
 *   gain/roi_x/roi_y/roi_w/roi_h -> s_axi_control (AXI4-Lite)
 *
 * ⚠ 输出流的长度恒为 GESTURE_OUT_PIXELS (9216)，与输入分辨率无关；
 *   这是与 CNN 侧的契约，见 docs/architecture-contract.md §3.1。
 */
void gesture_preproc(hls::stream<axis_rgb_t>  &src,
                     hls::stream<axis_gray_t> &dst,
                     int width,
                     int height,
                     int thresh_mode,
                     int thresh_offset,
                     int gauss_en,
                     int sobel_en,
                     int morph_en,
                     int gain,
                     int roi_x,
                     int roi_y,
                     int roi_w,
                     int roi_h);

#endif /* GESTURE_PREPROC_H */
