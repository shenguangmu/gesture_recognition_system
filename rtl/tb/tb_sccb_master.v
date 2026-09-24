// =====================================================================
//  tb_sccb_master.v —— sccb_master 自检 testbench
//
//  运行：
//      iverilog -g2012 -o tb_sccb.vvp tb_sccb_master.v ../sccb_master.v
//      vvp tb_sccb.vvp
//
//  ⚠ 必须看到 *** TB PASSED ***，"finished" 不算数。
//
//  ─────────────────────────────────────────────────────────────────
//  设计取舍：为什么解码器这么写
//  ─────────────────────────────────────────────────────────────────
//  前一版 TB 试图实现一个"完整 I2C 从机"（含 START/STOP 检测、ACK 驱动、
//  相位对齐），结果在从机自身的时序细节上反复出错 —— 那是**测试脚手架
//  的 bug**，不是被验对象的 bug，修它没有收益。
//
//  本版改用最直白的解码方式：
//
//      START 之后，每 9 个 SCL 上升沿构成一个字节
//      （前 8 个是数据位，第 9 个是 ACK 位）
//
//  这个模型不需要从机驱动 ACK，也不需要复杂的相位推理 ——
//  它只回答一个我们真正关心的问题：**主机发出的位串对不对**。
//
//  ACK 检查单独用"负向测试"覆盖：SDA 恒高（无人应答）时
//  cfg_error 必须置位。这样两个目标各自独立，互不干扰。
// =====================================================================

`timescale 1ns / 1ps

module tb_sccb_master;

    localparam CLK_PERIOD = 10;
    localparam CLK_DIV    = 10;      // 仿真加速（真实用 500）
    localparam DEV_ADDR   = 7'h3C;
    localparam N_REGS     = 4;
    localparam SUB_ADDR_BYTES = 2;   // OV5640：16 位寄存器地址
    localparam N_BYTES    = 1 + SUB_ADDR_BYTES + 1;

    reg clk = 0, rst_n = 0;
    always #(CLK_PERIOD/2) clk = ~clk;

    // ------------------------------------------------------------------
    //  DUT
    // ------------------------------------------------------------------
    wire        scl, sda_oe, sda_o;
    wire        sda_i;
    wire        cfg_done, cfg_error;
    wire [7:0]  done_cnt;
    wire [7:0]  fa_dut, nc_dut;      // 失败定位（2026-09-24 新增端口）
    reg  [7:0]  tbl_addr;
    wire [23:0] tbl_data;

    // 配置表：{子地址[15:0], 数据[7:0]}
    wire [23:0] reg_table [0:N_REGS-1];
    assign reg_table[0] = 24'h30_35_11;
    assign reg_table[1] = 24'h30_36_3C;
    assign reg_table[2] = 24'h3C_07_08;
    assign reg_table[3] = 24'h50_3D_80;
    assign tbl_data = reg_table[tbl_addr[1:0]];

    // CHK_ACK 通过参数开关：本 TB 只做"内容"与"NACK 检测"两件事，
    // 不需要从机应答，所以固定为 0，让内容校验跑到底。
    sccb_master #(
        .CLK_DIV  (CLK_DIV),
        .DEV_ADDR (DEV_ADDR),
        .N_REGS   (N_REGS),
        .CHK_ACK  (0)
    ) u_dut (
        .clk       (clk),
        .rst_n     (rst_n),
        .tbl_addr  (tbl_addr),
        .tbl_data  (tbl_data),
        .scl       (scl),
        .sda_oe    (sda_oe),
        .sda_o     (sda_o),
        .sda_i     (sda_i),
        .cfg_done  (cfg_done),
        .cfg_error (cfg_error),
        .done_cnt  (done_cnt),
        .first_err_addr (fa_dut),
        .nack_cnt       (nc_dut)
    );

    // ------------------------------------------------------------------
    //  开漏总线：主机拉低条件 = sda_oe && !sda_o
    //  本 TB 不驱动从机，SDA 由上拉决定（主机不拉低就是高）
    // ------------------------------------------------------------------
    assign sda_i = (sda_oe & ~sda_o) ? 1'b0 : 1'b1;

    // ------------------------------------------------------------------
    //  第二个 DUT：CHK_ACK=1 且 SDA 恒高（模拟"无人应答"）
    //
    //  ⚠ 必须放在顶层例化，不能写进 initial 块 ——
    //    Verilog 不允许在 initial 中间声明 wire。
    // ------------------------------------------------------------------
    wire        scl2, oe2, o2, done2, err2;
    wire [7:0]  dc2, fa2, nc2;
    reg  [7:0]  ta2;
    wire [23:0] td2;
    assign td2 = reg_table[ta2[1:0]];

    sccb_master #(
        .CLK_DIV(CLK_DIV), .DEV_ADDR(DEV_ADDR),
        .N_REGS(N_REGS), .CHK_ACK(1)
    ) u_neg (
        .clk(clk), .rst_n(rst_n),
        .tbl_addr(ta2), .tbl_data(td2),
        .scl(scl2), .sda_oe(oe2), .sda_o(o2),
        .sda_i(1'b1),                    // 总线恒高 = 无人应答
        .cfg_done(done2), .cfg_error(err2), .done_cnt(dc2),
        .first_err_addr(fa2), .nack_cnt(nc2)
    );

    // ------------------------------------------------------------------
    //  第三个 DUT：**部分 NACK** —— 全 NACK 只能证明"计数在涨"，
    //  证明不了"首错地址是第一个失败的那条"。
    //
    //  做法：在 sda_i 上挂一个组合判断 —— 只有当前正在配的
    //  tbl_addr 属于 {1, 3} 时拉高（模拟这两条寄存器被拒），
    //  其余照常应答。这样期望值是可精确预测的：
    //        first_err_addr == 1   （首个失败是第 1 条）
    //        nack_cnt       == 2   （第 1、3 条各一次）
    //  ⚠ 之所以能直接看 tbl_addr 判断"当前配到哪一条"：
    //    S_NEXT 里 tbl_addr 是**配完之后**才自增的，所以整个事务期间
    //    tbl_addr 恒为当前事务的索引。
    // ------------------------------------------------------------------
    localparam N_REGS3 = 6;
    wire        scl3, oe3, o3, done3, err3;
    wire [7:0]  dc3, fa3, nc3;
    reg  [7:0]  ta3;
    wire [23:0] td3;
    assign td3 = reg_table[ta3 % N_REGS];

    // 被拒的条目：第 1 和第 3 条
    // ⚠ 极性不能反！sda_i **低** = 从机拉低 = ACK（成功）；
    //   sda_i **高** = 无人应答 = NACK（失败）。
    //   （SCCB/I2C 的 ACK 是低有效。第一版这里写成取反，
    //    结果把{1,3}变成了"被应答的"，两个断言全挂 —— 是**测试**的错。）
    wire sda_i3 = (ta3 == 8'd1) || (ta3 == 8'd3);

    sccb_master #(
        .CLK_DIV(CLK_DIV), .DEV_ADDR(DEV_ADDR),
        .N_REGS(N_REGS3), .CHK_ACK(1)
    ) u_partial (
        .clk(clk), .rst_n(rst_n),
        .tbl_addr(ta3), .tbl_data(td3),
        .scl(scl3), .sda_oe(oe3), .sda_o(o3),
        .sda_i(sda_i3),
        .cfg_done(done3), .cfg_error(err3), .done_cnt(dc3),
        .first_err_addr(fa3), .nack_cnt(nc3)
    );

    // ------------------------------------------------------------------
    //  采样：全部先打一拍，避免用组合值判沿
    // ------------------------------------------------------------------
    reg s1, s2, d1, d2;
    always @(posedge clk) begin
        if (!rst_n) begin
            s1 <= 1'b1; s2 <= 1'b1; d1 <= 1'b1; d2 <= 1'b1;
        end else begin
            s1 <= scl;   s2 <= s1;
            d1 <= sda_i; d2 <= d1;
        end
    end

    wire scl_rise = s1 & ~s2;
    wire sda_fall = ~d1 & d2;
    wire sda_rise = d1 & ~d2;

    // START/STOP 要求 SCL 稳定为高（本拍与上拍都是高）。
    // 主机在 SCL 下降沿改 SDA，若不加这个限定会把每次数据变化误判成 START。
    wire scl_hi = s1 & s2;
    wire start_c = scl_hi & sda_fall;
    wire stop_c  = scl_hi & sda_rise;

    // ------------------------------------------------------------------
    //  位串解码：START 后每 9 个 SCL 上升沿一个字节
    // ------------------------------------------------------------------
    reg  [7:0]  sh    = 8'd0;    // 当前字节的移位缓存
    reg  [7:0]  nb    = 8'd0;    // 自 START 起已过的 SCL 上升沿数
    reg         inframe = 1'b0;

    integer     txn_cnt = 0;
    reg [31:0]  txn_got [0:N_REGS-1];

    always @(posedge clk) begin
        if (!rst_n) begin
            sh <= 8'd0; nb <= 8'd0; inframe <= 1'b0;
        end else if (start_c) begin
            inframe <= 1'b1;
            nb      <= 8'd0;
            sh      <= 8'd0;
        end else if (stop_c) begin
            inframe <= 1'b0;
            nb      <= 8'd0;
        end else if (inframe && scl_rise) begin
            if ((nb % 9) < 8) begin
                // 数据位：左移进 sh
                sh <= {sh[6:0], d1};
                if ((nb % 9) == 7) begin
                    // 一个字节收齐，追加到事务字里（MSB first）
                    txn_got[txn_cnt % N_REGS] <=
                        {txn_got[txn_cnt % N_REGS][23:0], sh[6:0], d1};
                end
            end

            if ((nb % 9) == 8) begin
                // 一个字节（含 ACK 位）走完
                if (((nb + 1) / 9) == N_BYTES) begin
                    // 整个事务结束
                    txn_cnt <= txn_cnt + 1;
                    nb      <= 8'd0;
                end else begin
                    nb <= nb + 8'd1;
                end
            end else begin
                nb <= nb + 8'd1;
            end
        end
    end

    // ------------------------------------------------------------------
    //  比对
    // ------------------------------------------------------------------
    integer pass_cnt = 0;
    integer fail_cnt = 0;
    integer i;

    task check;
        input             ok;
        input [8*56-1:0]  name;
        begin
            if (ok) pass_cnt = pass_cnt + 1;
            else begin
                fail_cnt = fail_cnt + 1;
                $display("  FAIL : %0s", name);
            end
        end
    endtask

    // ------------------------------------------------------------------
    //  主流程
    // ------------------------------------------------------------------
    initial begin
        $dumpfile("wave_sccb.vcd");
        $dumpvars(0, tb_sccb_master);

        $display("=== SCCB TB START ===");

        rst_n = 0;
        repeat (20) @(posedge clk);
        rst_n = 1;

        // =============================================================
        //  用例 1：位串内容 —— 从机视角逐个字节校验
        // =============================================================
        $display("\n[1] 配置表全量下发 —— 位串内容校验");
        wait (cfg_done == 1'b1);
        repeat (100) @(posedge clk);

        check(done_cnt == N_REGS[7:0], "事务数应等于表长");
        check(txn_cnt  == N_REGS,      "解码到的事务数应等于表长");
        $display("      DUT 计数 %0d，解码到 %0d 个事务", done_cnt, txn_cnt);

        // ⚠ u_dut 的 CHK_ACK=0（不做 ACK 检查），所以这里**不该**有 NACK。
        //   断言哨兵值保持 —— 这条证明新加的失败定位**不会误报成功路径**。
        check(fa_dut === 8'hFF, "无 NACK 时 first_err_addr 应保持哨兵 0xFF");
        check(nc_dut == 8'd0,   "无 NACK 时 nack_cnt 应为 0");

        for (i = 0; i < N_REGS; i = i + 1) begin
            // 期望：{DEV_ADDR[6:0], 0(W), 子地址[15:0], 数据[7:0]}
            if (txn_got[i] !== {DEV_ADDR, 1'b0, reg_table[i]}) begin
                fail_cnt = fail_cnt + 1;
                $display("  FAIL : 事务%0d 位串不符  实收 %08h  应为 %08h",
                         i, txn_got[i], {DEV_ADDR, 1'b0, reg_table[i]});
            end else begin
                pass_cnt = pass_cnt + 1;
                $display("      事务%0d: %08h  OK", i, txn_got[i]);
            end
        end

        // =============================================================
        //  用例 2：SDA 恒高（无人应答）时 cfg_error 必须置位
        // =============================================================
        //  这是**负向测试**：只测正常路径无法证明错误路径有效。
        //  真实场景：摄像头没接好 / 缺上拉电阻 → 没有 ACK。
        //  用一个独立例化、CHK_ACK=1 的 DUT 来验，避免与用例 1 纠缠。
        $display("\n[2] 负向测试 —— 无 ACK 时 cfg_error 应置位");
        // u_neg 例化在顶层（见文件上方），其 sda_i 恒为 1，必然收到 NACK
        repeat (40000) @(posedge clk);
        check(err2 === 1'b1, "无 ACK 时 cfg_error 应置位");

        // ⚠ 这里**不应**断言 cfg_done == 0。
        //   本模块的既定行为是"遇到 NACK 记录错误但继续跑完整个表"，
        //   因为一条寄存器失败不代表其余也会失败，全跑完再报告更有用。
        //   所以 cfg_done 依然会置位，cfg_error 才是"是否有过 NACK"的指示。
        //   （早期版本的 TB 断言了 cfg_done==0，那是**测试写错了**，
        //     不是 DUT 的问题 —— 加断言时先想清楚模块的既定语义。）
        check(done2 === 1'b1, "跑完整张表后 cfg_done 应置位（即使有 NACK）");

        // ⚠ 全 NACK 时：首错地址必须是**第 0 条**（0x00，不是哨兵 0xFF），
        //   计数必须是表长。这正是"总线层问题"的特征签名。
        //   ⚠ nack_cnt 单位是**事务**不是字节 —— 1 条坏寄存器记 1，
        //     不是 4（见 sccb_master.v S_ACK 里的说明）。
        check(fa2 === 8'h00,       "全 NACK 时 first_err_addr 应为 0x00（第一条）");
        check(nc2 == N_REGS[7:0],  "全 NACK 时 nack_cnt 应等于表长（事务数）");

        // =============================================================
        //  用例 4：**部分 NACK** —— 首错地址应精确定位到第一条失败的
        // =============================================================
        //  这是本次新增功能的核心用例。全 NACK（用例 2）只能证明计数在涨，
        //  证明不了"定位得准"—— 因为首错地址在那种情况下必然是 0。
        //  u_partial 让 {1, 3} 两条 NACK，期望 first_err_addr==1、nack_cnt==2。
        $display("\n[4] 部分 NACK —— first_err_addr/nack_cnt 应精确定位");
        repeat (60000) @(posedge clk);
        check(err3 === 1'b1,        "部分 NACK 时 cfg_error 应置位");
        check(fa3 === 8'h01,        "首错地址应为 0x01（第一条失败的）");
        check(nc3 === 8'd2,         "NACK 计数应为 2（第 1、3 条）");
        check(dc3 == N_REGS3[7:0],  "事务计数仍应跑完整张表（既定语义）");
        $display("      first_err_addr=0x%02h  nack_cnt=%0d  表长=%0d",
                 fa3, nc3, N_REGS3);

        // =============================================================
        //  用例 3：复位后状态清零
        // =============================================================
        $display("\n[3] 复位 —— 状态应清零");
        rst_n = 0;
        repeat (10) @(posedge clk);
        check(cfg_done === 1'b0, "复位后 cfg_done 应为 0");
        check(done_cnt == 8'd0,  "复位后 done_cnt 应为 0");
        rst_n = 1;

        // =============================================================
        //  汇总
        // =============================================================
        $display("\n=== TB DONE: %0d passed, %0d failed ===", pass_cnt, fail_cnt);
        if (fail_cnt == 0)
            $display("*** TB PASSED ***");
        else
            $display("*** TB FAILED ***");

        $finish;
    end

    // ---- 超时保护 ----
    initial begin
        #40_000_000;
        $display("\n*** TB TIMEOUT ***");
        $display("=== TB DONE: %0d passed, %0d failed ===", pass_cnt, fail_cnt);
        $finish;
    end

endmodule
