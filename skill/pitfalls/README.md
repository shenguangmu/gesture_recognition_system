# 踩坑清单 —— 工具静默失败的定位方法

> **为什么这份清单值得读**：这些坑的共同特征是
> **工具不报错，或者报错位置离真正原因很远**。
> 每一条都给出**验证命令** —— 不靠推理，靠输出。

## 怎么用

按**现象**查，不按原因查。你手里只有现象。

| 现象 | 去哪条 |
|---|---|
| 综合/实现全过，但 DRC 报一堆莫名其妙的端口名 | **P1** |
| 加了"失败重试"，日志显示重试了但同一秒返回 | **P2** |
| 脚本跑到最后突然 `No open design`，产物缺失 | **P3** |
| OOC 综合日志里出现 `Failed to create directory 'C'` | **P4** |
| 改了源码，报告里的旧数字还在，没人发现 | **P5** |
| 测试脚本打了 `[FAIL]` 却报 `PASSED` | **P6** |
| 渲染/导出漏了一种块类型，内容静默消失 | **P7** |
| 测试静默跳过，覆盖率缩水没人知道 | **P8** |
| **修好了 P6 的同一个坑，换个位置又犯一遍** | **P9** |
| **AXI DMA 传大块时"静默只传了前 16KB"，上板卡死** | **P10** |
| 综合通过但上板数据不对 | 见 `docs/board-bringup-guide.md` 的分级验证 |
| HLS 相关（pragma 被丢、csim 假成功…） | 见附录索引 |

---

## P1 · 顶层被静默设成子模块，整条流水线未进综合

**现象**

```
[DRC NSTD-1] 67 out of 67 logical ports use IOSTANDARD 'DEFAULT'
问题端口: cam_data[7:0], frame_cnt[15:0], aclk, pclk ...
```

端口名**看着像某个子模块的**，不是你的顶层。
日志里是 `Command: synth_design -top <子模块名>`。

**根因**

Block Design 生成时会**重新生成 wrapper**，把它从工程文件表里挤掉。
Vivado 只好在剩余模块里**自动挑一个当顶层** —— 挑中了某个 RTL 子模块。

**为什么难查**：**全流程静默通过**。综合、实现、出比特流都不报错。

**对策**

**必须回读断言**，不能靠"没报错就当对了"：

```tcl
set top_now [get_property top [current_fileset]]
if {$top_now ne "${BD_NAME}_wrapper"} {
    error "顶层设置失败：期望 ${BD_NAME}_wrapper，实际 '$top_now'"
}
if {[llength [get_files -quiet *${BD_NAME}_wrapper.v]] == 0} {
    error "wrapper 未登记进工程文件表"
}
```

**验证**

```bash
grep "Command: synth_design" <project>.runs/synth_1/runme.log
# 必须是你的顶层名。是子模块名 → 中招了
```

---

## P2 · `launch_runs` 对**已失败**的 run 不做任何事 → 重试是假的

**现象**

给 run 加了"失败就重跑"的重试，日志显示重试了，但**同一秒就返回**：

```
>>> synth_1 进度 0% —— 第 1 次未完成
>>> 重试...
[21:38:34] Waiting for synth_1 to finish...
[21:38:34] synth_1 finished        ← 同一秒返回，根本没跑
```

**根因**

`launch_runs` 认为那个 run "已经跑过了"（只是结果是失败），**于是什么都不做**。
光靠"再 launch 一次"是**假重试**。

**对策**

重试前先 `reset_run`（**已完成的子 run 不受影响**，只有失败的需要重跑）：

```tcl
if {$attempt > 1} { catch {reset_run -quiet $run_name} }
catch {launch_runs $run_name -jobs 8 {*}$launch_args}
catch {wait_on_run $run_name}
```

**⚠ 配套坑**：找失败子 run **不能用** `get_runs "${run_name}_*"` ——
run 叫 `synth_1`，子 run 却叫 `bd_video_dma_in_0_synth_1`，**glob 匹配不到**，
那段诊断会**静默失效**（你以为在报错，其实一行没输出）：

```tcl
# 错：匹配不到任何东西
foreach sr [get_runs -quiet "${run_name}_*"] { ... }

# 对：遍历全部，按后缀筛，排除父 run
foreach sr [get_runs] {
    set sn [get_property NAME $sr]
    if {$sn eq $run_name} { continue }
    if {[string match "*_$run_name" $sn] && [get_property PROGRESS $sr] ne "100%"} { ... }
}
```

> **这条的教训**：桩函数能验证"重试被调用了"，但**验证不了
> `launch_runs` 的真实语义**。控制流对 ≠ 行为对。

---

## P3 · `get_timing_paths` 需要**已打开的设计**

**现象**

脚本跑到 "实现完成" 后突然中断：

```
>>> 实现完成
ERROR: [Common 17-53] User Exception: No open design.
```

**后果**：后面的产物导出**根本没执行到**。
而 `write_bitstream completed successfully` 就在日志里 —— **容易误以为"都成功了"**。

**根因**

`get_timing_paths` 写在 `open_run impl_1` **之前**。

**对策**

```tcl
open_run impl_1        # ← 必须在前面

# 时序查询本身也加 catch —— 它只是**报告**，不该有中断整个流程的能力
if {[catch {
    set wns [get_property SLACK [get_timing_paths -delay_type max]]
    puts ">>> WNS = $wns ns"
} err]} {
    puts "WARN: 取时序失败（不影响产物）: $err"
}
```

**验证**

判据不是"脚本没报错"，而是**产物在不在**：

```bash
ls -la <project>/<name>.xsa <project>/utilization.rpt
```

> **这条的教训**：脚本"跑到最后一行"和"所有产物都生成了"是**两回事**。

---

## P4 · `Failed to create directory 'C'` —— 产物无害，**流程致命**

**现象**

OOC 综合日志里出现（**每次打中的 run 都不一样**，是随机的）：

```
ERROR: [Common 17-354] Could not open 'C' for writing.
ERROR: [Common 17-1257] Failed to create directory 'C'.
```

看起来像环境变量问题（某个变量为空、被展开成裸盘符 `C`），**但不是**。

**根因**

一次性启动 16–22 个 OOC 综合，每个 Vivado 实例都要建一批临时目录 ——
**并发建目录竞争的瞬时失败**。重跑必然成功
（中招的 run 自己的 `.dcp` 其实照样生成了）。

**已排除**（这些查过都不是原因）：
- `TEMP` / `TMP` 正常
- 无空环境变量会被展开成裸盘符
- 脚本里没有任何地方传过 `"C"`

**⚠ 为什么不能当噪声忽略**

Vivado 会把这个瞬时失败**判定成 run 失败**：

```
ERROR: [Vivado 12-13638] Failed runs(s) : '<run 名>'
ERROR: [Common 17-39] 'wait_on_runs' failed due to earlier errors.
```

于是 `wait_on_run` **直接抛错返回**，脚本中断 —— **后面的产物根本导不出来**。

**对策**：带 `reset_run` 的重试（见 **P2**）。

**验证**

```bash
grep -c "17-1257\|17-354" <run>/runme.log   # 出现了几条
grep -c "^ERROR" <impl>/runme.log            # 但整体 ERROR 应为 0
```

---

## P5 · 报告里的数字比源码旧 —— 改了代码，没人发现

**现象**：你改完源码、跑完综合、去读报告里的资源数，
**读到的是上一次运行的**。报告不会自己告诉你它过期了。

**本项目实际发生的**：
做 DSP 对照实验时来回改了四次源码，最后一次（展开版，86 DSP）跑完
就去写文档了；等回头核对数字时，`csynth.rpt` 里躺着的还是**85 实验版**的数，
而源码已经 `git checkout` 回基线了。
**如果当时没顺手多跑一次，写进报告的就是一组对不上源码的数字。**

**为什么危险**：这类错误的后果和 P2 一样 ——
**看起来一切正常**。数字是"真的"（确实是工具产出的），
只是**不是当前源码的**。csim 会拦住代码错误，**谁也拦不住这个**。

**验证命令**：

```bash
# ① 比时间戳：报告比源码旧，就说明报告过期了
ls -l --time-style=+%m-%d_%H:%M src_hls/gesture_preproc.cpp \
      gesture_comp/solution1/syn/report/csynth.rpt
#         源码必须比报告旧（或同一时刻）

# ② 比 git 状态：源码有未提交改动，报告必然对不上
git status --short src_hls/
```

**根治**：**不要在"源代码有未提交改动"的状态下引用报告数字**。
要么先 `git stash`/`checkout` 到目标状态重跑，要么在提交后重跑一次。

> **更一般的教训**：**引用一个数字前，先确认它属于哪次运行。**
> 这和"引用前先确认它属于哪个模块"（`WNS +0.265` 的教训）是同一类问题 ——
> 数字的**出处**和数字本身一样重要。

---

## P6 · 测试脚本打了 `[FAIL]` 却报 `PASSED`

**现象**：自检脚本明明打印了一行 `[FAIL] ...`，
最后汇总却是 `*** TEST PASSED ***`，退出码 0。CI 于是放行。

**根因**：失败路径**没有走计数器**。典型写法：

```python
def step2():
    try:
        from pynq import allocate
    except ImportError:
        print("    [FAIL] 连 pynq 都 import 不了")
        return False          # ← 打印了，但 _failed 没加一
```

主流程只在 `_failed` 里统计，而这个早退分支跳过了它 ——
**"报错"和"记账"是两条独立的路径，只做了前者。**

**本项目实际发生的**：写 `host/bringup_check.py` 时第一版就是这样，
在 PC 上试跑 `--step 2` 才抓到（无板环境必然 import 失败，正好走这条分支）。
**如果没有"无板也能跑一遍"这一步，这个 bug 会一直留到上板那天**，
而且表现是"明明失败却告诉你成功"—— 比直接崩掉危险得多。

**对策**：**所有失败路径都走同一个记账函数**，不允许裸 `return False`：

```python
def _fail():
    global _failed
    _failed += 1
    return False

# 所有早退分支统一写成
    print("    [FAIL] ...")
    return _fail()
```

**验证命令**（这条才是关键 —— 光看代码看不出来）：

```bash
# 故意制造一个必然失败的场景，看汇总结论对不对
python host/bringup_check.py --step 2   # 在无 pynq 的 PC 上跑
# 期望：打印 FAILED，且 echo $? == 1
```

> **一般化的教训**：**测试脚本本身也要被测。**
> 而且最省事的测法是**让它跑一个必然失败的场景** ——
> 正常路径谁都会测，**失败路径才是没人走的那条**。

---

## P7 · 渲染/导出漏了一种块类型，内容**静默消失**

**现象**：源文件里明明有的内容，生成出来的 Word / PDF / 静态站点里**没有**。
而且**不报任何错** —— 生成器照常打印"已完成"。

**根因**：生成器把源文件解析成若干**块类型**，
然后按类型分别渲染。**新加了一种块类型，却只在一个地方处理了它。**

**本项目实际发生的**（2026-09-19）：

给上板手册加"在哪跑"标记 `【终端：PC · Git Bash】`，
解析器产出了 `terminal` 块类型，但渲染器里那段处理写在了
`elif t == 'para'` 分支里 —— 而 `terminal` 是**独立类型**，走不到那一支。

结果：**17 个标记一个都没渲染出来，全被丢掉**。

更隐蔽的是**误导性的"部分成功"**：手册引言的代码块里正好有 4 行
**示例文字**也是 `【终端：…】` 的形状，它们作为代码内容被正常渲染了 ——
于是 grep 一下能搜到 4 个，看着像"功能是好的，只是少了几个"。

**怎么抓到的**：不是靠眼看，是靠一条**内容零丢失**的自动检查 ——
把源文件和产出的所有文字都摊平，反向核对每一行还在不在。
`scripts/test_manual.py` 第 [3] 项就是这个。

**对策**：

1. **加块类型时，把"渲染分支"和"测试的摊平函数"一起改。**
   本项目就被这个坑咬了两次：`iter_text()` 漏了 `terminal` 分支
   （导致测试误报"内容丢失"），`render()` 也漏了（真丢内容）。
2. **写一条"零丢失"检查**，别靠"生成成功"当判据。
   它抓的是**所有**静默丢失，不只是你想到的那一种。
3. ⚠ **两处渲染路径都要维护**：本项目有"主体渲染"和"引用块渲染"
   两条路径，只改一条 → 表现为"某些位置的标记不显示"，同样不报错。

> **一般化**：**生成器/编译器/导出器最危险的失败模式是"不报错、只少东西"。**
> 编译器至少有类型系统兜底，而**文档生成器什么都没有** ——
> 它丢一段和你故意删一段，产出是一样的。

---

## P8 · 测试静默跳过 —— 汇总照样报 PASSED

**现象**：测试跑完打印 `*** TESTS PASSED ***`，但**其中一部分根本
没执行**。退出码 0，CI 放行。

**根因**：测试里有"环境不具备就跳过"的分支，而跳过**只 print 了一句**、
没进统计。汇总只看 `_failed`，于是：

    10 项里跑了 7 项 → 汇总仍是 PASSED → 没人知道少了 3 项

**本项目实际发生的**（2026-09-19）：

`test_pynq_serial.py` 的串口筛选测试写了：

```python
try:
    from serial.tools import list_ports
    PortInfo = list_ports.ListPortInfo      # ← 这一行在 pyserial 3.5 必抛
except Exception:
    print("(跳过：本机没有 pyserial)")
    return
```

**`ListPortInfo` 在 pyserial 3.5 里不在 `list_ports` 顶层**
（在 `list_ports_common` 里）。所以哪怕本机装了 pyserial，
那 3 项也**一直在静默跳过** —— 而且修的时候才发现，
它们的构造函数写法**本身就是错的**（`ListPortInfo()` 缺必需参数），
**因为从没真跑过，这个错一直没暴露**。

**怎么发现的**：给跳过加了**计数器**，汇总变成
`PASSED (7 项，跳过 3 项)` —— 那一刻"跳过"才变得可见。
之前它只是一行在滚动输出里被淹没的字。

**对策**：

1. **跳过必须计数并出现在汇总里**，不能只 print 一句。
   汇总要能一眼看出"这次到底测了多少"。
2. **CI 里把"有跳过"当失败**。本项目现在直接
   `grep -q '跳过' && exit 1` —— 覆盖率缩水不该悄悄发生。
3. **补上缺的依赖让测试真跑**（本项目让 CI 装 pyserial），
   比让测试优雅跳过要好。

> **一般化**：**"跳过"是一种和"通过"长得一样的失败。**
> 它更危险 —— 通过至少说明代码是对的，
> 而跳过只说明**这次没检查**。
> 条件跳过本身没错，错的是**跳过之后不报告**。

---

## P9 · 修好了 P6 的**同一个 bug**，换个位置又犯一遍

> **来源**：2026-09-21 首次上板实测。这一条是**AI 协作时真实发生的**，
> 记下来是因为它揭示了 P6 没说到的后半句。

**现象**

`host/bringup_check.py` 在板上跑，`[3a]` 明明打了：

```
    [FAIL] overlay 加载/认 IP 失败：Overlay.__init__() missing 1 required
           positional argument: 'bitfile_name'
```

最后却汇总成：

```
  *** BRINGUP CHECK PASSED ***  (4 项检查全过)
```

**根因**

**和 P6 一模一样**：失败路径是裸 `return False`，没走 `_fail()`。

```python
    except Exception as e:
        print("    [FAIL] overlay 加载/认 IP 失败：%s" % e)
        return False          # ← 打印了，但 _failed 没加一
```

**为什么值得单列一条**

`bringup_check.py` 这个文件里，**`_fail()` 的实现、它的 docstring、
以及"第一版就是这样、被 `--step 2` 试跑抓到"的记录全都在**（见 P6）。
也就是说：**作者知道这个坑，把对策写进了同一个文件，
然后在同一个文件里又犯了一次** —— 只是换到了 `[3a]`/`[3c]`/`[3d]` 分支。

**P6 只说了"要修"，没说"怎么保证不再犯"。这一条补上后半句。**

**对策**

1. **不要靠自觉，靠扫描**。一条命令就能查：

   ```bash
   grep -n "return False" host/bringup_check.py
   # 期望：只命中 _fail() 函数体内的那一行
   ```

   修复后应为 **5 处 `return _fail()`** + `_fail()` 内部 1 处 `return False`。

2. **测试必须覆盖失败路径**（正常路径谁都会测）：

   ```bash
   python host/bringup_check.py --step 3   # 无板环境 → [3a] 必然失败
   # 期望：*** BRINGUP CHECK FAILED *** 且 exit code == 1
   ```

   ⚠ **但这还不够** —— 它只证明"报了 FAILED"，
   **没证明"这次是 `_fail()` 记的账"**（函数返回 False 也会走到汇总）。
   **差分测试才严谨**：

   ```bash
   # 把 return _fail() 临时改回 return False，重跑
   #   → 期望：错误地打印 PASSED 且 exit code == 0   ← 复现出 bug
   # 改回去再跑
   #   → 期望：FAILED 且 exit code == 1              ← 证明修复有效
   ```

   本项目 2026-09-21 实测结果：

   | 版本 | 汇总输出 | 退出码 |
   |---|---|---|
   | 裸 `return False` | `PASSED (0 项检查全过)` | `0` ❌ |
   | `return _fail()` | `FAILED (1 失败)` | `1` ✅ |

   **同一个失败场景，两种写法给出相反结论 —— 这就是证据。**

> **一般化**：**"知道一个坑"和"不会再踩"是两件事。**
> 把对策写进文档，不等于把对策写进检查。
> 凡是"漏记账"类的 bug，**唯一可靠的防线是自动扫描 + 差分测试** ——
> 因为它的表现是"看起来成功了"，人不会去怀疑它。

---

## P10 · AXI DMA 传大块时**静默只传前 16 KB**，下游 IP 卡死

> **来源**：2026-09-21 手势识别项目首次上板实测。**本文中排查耗时最长的一条**
> —— 从"怀疑硬件坏"一路查到 BD 里一个从没设过的 IP 参数。

**现象**

上板跑「DMA → HLS 流水线 → DMA」，`ap_done` **永不置位**，10 秒超时。
但所有其它环节看起来都是好的：

```
② DDR 自检             ✅ 4/4
③a 认 IP               ✅ 6 个全认到，地址与 .hwh 逐条吻合
③c 写参数 + 回读        ✅ thresh_offset 回读 == -8
     CTRL = 0x01       ✅ ap_start=1, ap_idle=0  → IP 确实在跑
     dma_in  IOC_Irq   ✅ 输入端"传输完成"
     dma_out 无 IOC    ❌ 输出端一个字节都没收到
```

**报错信息一条都没有。** 只有 `ap_done` 超时。

**根因**

`vivado/bd_video.tcl` 里例化 AXI DMA 时**没有设 `C_SG_LENGTH_WIDTH`**，
于是 IP 用了默认值 **14 位**：

```tcl
set dma_in [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma dma_in]
set_property -dict [list \
    CONFIG.c_include_sg              {0} \
    # ❌ 没有 c_sg_length_width —— 默认 14
] $dma_in
```

**14 位 = 单次传输最多 `2^14 - 1 = 16383` 字节。**
而这帧要传 **614400** 字节（640×480×RGB565），是上限的 **37 倍**。

**确诊的那条证据**（写一组值、读回，看规律）：

| 写入 LENGTH | 读回 | **写入 mod 16384** |
|---|---|---|
| 614400 | **8192** | 8192 ✅ |
| 123456 | **8768** | 8768 ✅ |
| 100000 | **1696** | 1696 ✅ |
| 65536 | **0** | 0 ✅ |
| 8192 | 8192 | 8192 ✅ |

**读回值 = 写入值 mod 16384 —— 长度字段只有 14 位，高位被直接丢弃。**

于是 DMA 只搬了前 16384 字节就置 `IOC_Irq`（**"传输完成"是真的，
只是它认为的"全部"只有 16 KB**），下游 IP 永远等不到剩下的输入 → 整条
DATAFLOW 链停摆 → `dst` 不吐 → `ap_done` 永不置位。

**为什么难查**

1. **不报错。** `IOC_Irq` 照常置位，"传输完成"看起来完全正常。
2. **csim / cosim 查不出来。** 仿真环境里没有真实的 AXI DMA 长度寄存器，
   这个限制**只在硬件上存在** —— 所以 `csim + csynth + cosim 全过` 毫无帮助。
3. **表现为"跑一帧超时"**，报错位置离真凶（BD 里一个 IP 参数）有十万八千里。
4. `C_SG_LENGTH_WIDTH` 这个名字有迷惑性 —— 它**看起来**只跟
   **Scatter-Gather 模式**有关（我们用的是 `c_include_sg=0` 的直连模式），
   所以很容易认为"不用 SG 就不用设它"。**实际上 Direct 模式也用这个字段
   来存放传输长度。**

**对策**

**显式设置，别用默认值**：

```tcl
CONFIG.c_sg_length_width  {24} \    # 24 位 = 16 MB 上限，留足余量
```

本项目两个 DMA 都设了（`dma_out` 只传 9216 B 虽没超限，但保持一致，
避免以后改输出尺寸时重踩）。

**验证命令**（改了 BD 之后必做）：

```bash
# 1. 确认参数真的进了硬件描述（不是只写在 tcl 里）
unzip -p <XSA> bd_video.hwh | grep -i "SG_LENGTH_WIDTH"
#    期望：C_SG_LENGTH_WIDTH = 24

# 2. 上板后，写一个**超过 16383** 的长度再读回
#    写 614400 → 读回必须是 614400
#    读回 8192（mod 16384）就是没生效
```

> **一般化**：**BD 里凡是"传输长度/地址宽度"类的参数，都不要依赖 IP 默认值。**
> 默认值是给"小 demo"准备的，而本项目的帧尺寸（640×480）远超默认假设。
> 这类值的特点是：**综合不报错、仿真不报错、上板才坏，而且坏得很安静。**
>
> 更普遍地说 —— **仿真通过 ≠ 硬件可用**，因为仿真里没有真实的
> 总线宽度、FIFO 深度、寄存器位宽这些"物理约束"。
> （对照 **C1** 的 HLS pragma 被丢，是同一类"csim 看不见"的坑。）

---

## 附录：更完整的清单

本目录只列了**"工具流程全过、只有上板才暴露"**这一类中最典型的 9 条
（P9 / P10 是 2026-09-21 上板实测后补的）。
更完整的 15 条（HLS 接口/pragma、Vitis 命令行、BD/Tcl、Zynq 协同、环境噪声）
见项目仓库 `vivado/README.md` 的「踩过的 19 个坑」一节，
其中前 15 条按 A–E 五类组织。

**判断一条报错要不要管的通用方法**：

```bash
# 只看这两种级别
grep -E "ERROR|CRITICAL WARNING" <runme.log>
# 数一下
grep -c "^ERROR" <runme.log>
```

> 一个 33 KB 的 `vivado.log` 里有 **111 条 WARNING 但 0 条 ERROR** 是**正常**的。
> 别因为刷屏就以为工程有问题 —— 但也别反过来，把 ERROR 当噪声。
> **P4 就是后者**：它对产物无害，但对流程致命。
