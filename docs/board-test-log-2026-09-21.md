# 上板实测记录 — 2026-09-21

> **状态**：③「只跑预处理链」**已通过** ✅
> **根因**：BD 里 AXI DMA 的 `C_SG_LENGTH_WIDTH` 用了默认值 **14 位**，
> 单次传输上限 16383 字节，而输入帧要传 614400 字节 → 只传了前 16 KB
> → 下游 IP 等不到剩余输入 → 整条 DATAFLOW 卡死 → `ap_done` 永不置位。
>
> **修复**：`bd_video.tcl` 显式设 `CONFIG.c_sg_length_width {24}`（两个 DMA），
> 重新生成比特流。修复后 `*** BRINGUP CHECK PASSED ***`（11 项全过）。
> 详见 **`skill/pitfalls/README.md` P10**。
>
> 本文按时间顺序保留了**完整的排查过程**（含走错的弯路），
> 因为那些弯路本身就是"这类问题为什么难查"的说明。

> 本文是**当天实测的原始记录**，与 `board-bringup-guide.md` 的分工：
> 那份是「**该怎么做**」，本文是「**实际做出来是什么样**」。

---

## 一、结论速览

| 步骤 | 内容 | 结果 |
|---|---|---|
| 上电前检查（§2.1/§2.2） | 短路 / 引脚复核 | ⚠ **仍未做**（见 §6） |
| §3 PS 侧 | 串口登录 | ✅ |
| §4 加载 overlay | `.bit` + `.hwh` 加载、认 IP | ✅ |
| §5.1 ② DDR 自检 | buffer 分配 / 读写 | ✅ 4/4 |
| **§5.2 ③ 预处理链** | **跑一帧** | ✅ **11/11 全过** |
| §5.3–5.5 ④~⑧ | 摄像头相关 | ⬜ 未开始 |

**③ 的最终输出**：

```
启动回读: dma_in LENGTH=614400 (写 614400) OK | dma_out LENGTH=9216 (写 9216) OK
跑完一帧，耗时 0.005 s
    [ OK ] 输出不全黑（非零 2658/9216）
    [ OK ] 输出形状是 96x96
  *** BRINGUP CHECK PASSED ***  (11 项检查全过)
```

> 耗时校验：614400 B @ 100 MHz ≈ 3.1 ms 流水线 + 开销 ≈ **5 ms**，吻合。


---

## 二、环境踩坑记录（**手册里没有，值得补进去**）

这一节全部是**与项目代码无关**的环境问题，但每一个都会让人以为是硬件坏了。

### 2.1 SD 卡未烧录镜像

**现象**：上电后串口**完全无输出**，只有 PWR 灯亮。

**根因**：SD 卡是空的（未烧写 PYNQ 镜像）。

**⚠ 这里有个概念坑**：`board-bringup-guide.md:319` 写的是
「只有 PWR 灯亮 → 卡在 **FSBL** / 没加载」。**这句话是错的、会误导人**：

- PYNQ 走 **SD 卡启动**（PYNQ 镜像自带 FSBL + U-Boot + Linux）
- **PYNQ 镜像不会打印 FSBL banner**
- 因此「串口里没看到 FSBL」**不能**作为启动失败的判据

**正确判据**：串口有没有滚到 `login:`。

**解决**：balenaEtcher 烧 `pynq_z2_v3.0.1.img`（7,858,807,808 B）到 16GB SD 卡。

### 2.2 网络：USB 转 RJ45 的 IP 配错网卡（**最有迷惑性的一条**）

**现象**：`ping 192.168.2.99` 100% 丢包，但**板子网口指示灯黄绿交替闪**（说明板子侧正常）。

**根因**：笔记本有**两块网卡**：

| 适配器 | 描述 | 实际用途 |
|---|---|---|
| `以太网` | Motorcomm YT6801（内置 RJ45） | ❌ 没插任何东西 |
| **`以太网 2`** | **USB2.0 Ethernet Adapter** | ✅ **实际连接板子的那个** |

IP 被配到了 `以太网`（内置口）上，而链路实际走 `以太网 2`。

**⚠ 排查时容易犯的错**：查 `Get-NetAdapter -Name '以太网'` 看到
`Disconnected / 0 bps`，就断定「线没插」——**实际上是查错了网卡**。

**正确做法**：先 `Get-NetAdapter`（不带 `-Name`）**列出全部适配器**，
看哪个是 `Up` 且 `LinkSpeed` 非 0，那才是真正在用的。

**解决**：把 `192.168.2.100/24` 配到 `以太网 2`，内置口还原为 DHCP。

> 附：该 USB 网卡是 **100 Mbps**（非千兆），对本项目够用。

### 2.3 板卡端 Linux 环境（三个坑叠在一起）

③ 步跑不起来，**前三次失败全部是环境问题**，不是设计问题：

| # | 报错 | 根因 |
|---|---|---|
| 1 | `RuntimeError: Overlay is not downloaded` | 此版 PYNQ 要求**先加载 overlay**，`allocate()` 才能用 |
| 2 | `OSError: Root permissions required` | 加载 overlay / 访问 MMIO 需要 root |
| 3 | `ModuleNotFoundError: No module named 'pydantic'` | `sudo` **重置 PATH** → `python3` 指向系统 Python，而非 `/usr/local/share/pynq-venv/bin/python3` |
| 4 | `RuntimeError: No Devices Found`（`is the XRT environment sourced?`） | `sudo` **还重置了 `XILINX_XRT`** 环境变量 |

**3、4 是同一个根因的两种表现**（`sudo` 清空环境）。

**✅ 唯一正确的调用方式**：

```bash
sudo -E /usr/local/share/pynq-venv/bin/python3 <脚本> [参数]
```

**三个要素缺一不可**：

| 要素 | 为什么 |
|---|---|
| `sudo` | 要 root 权限 |
| `-E` | 保留 `XILINX_XRT` 等环境变量 |
| **解释器写全路径** | 绕开 `sudo` 的 PATH 重置，用 pynq-venv 的 Python |

**反例（都会失败）**：
```bash
sudo python3 xxx.py                    # ❌ 没 pydantic（PATH 被重置）
python3 xxx.py                         # ❌ 没权限
sudo /usr/.../python3 xxx.py           # ❌ No Devices Found（没 -E）
```

### 2.4 传输 / 粘贴的坑

| 现象 | 原因 |
|---|---|
| `scp: stat local "gesture_system.bit": No such file` | `.bit` 在 `.xsa` 里，**不是仓库里的普通文件**，要先 `unzip` |
| `scp` 报 `No such file`（源路径是 `/e/...`） | **在板子上跑了 `scp`** —— `scp` 必须在 PC 上跑 |
| `IndentationError: unexpected indent` | 多行命令粘贴时被终端折行 |
| `/homnx/gesture_system.bit` | 粘贴丢字符（`/home/xilinx/` 被截断） |

**规律**：`scp` 在 **PC**（提示符 `xiaomu@DESKTOP-...`），
跑 Python 在**板子**（提示符 `xilinx@pynq`）。

---

## 三、根因与修复（**最终结论**）

### 3.1 根因

`vivado/bd_video.tcl` 例化两个 AXI DMA 时**没设 `C_SG_LENGTH_WIDTH`**，
IP 采用默认值 **14 位** → 单次传输上限 `2^14 - 1 = 16383` 字节。

输入帧是 **614400** 字节（640×480×RGB565），**是上限的 37 倍**。
于是 DMA 只搬了前 16384 字节就置 `IOC_Irq`（"传输完成"是真的，
只是它认为的"全部"只有 16 KB）→ `crop_scale` 永远读不满输入
→ 整条 DATAFLOW 链停摆 → `dst` 不吐 → `ap_done` 永不置位。

### 3.2 确诊证据：`写入 mod 16384`

写一组长度值再读回，规律完全吻合：

| 写入 LENGTH | 读回 | **写入 mod 16384** |
|---|---|---|
| 614400 | **8192** | 8192 ✅ |
| 123456 | **8768** | 8768 ✅ |
| 100000 | **1696** | 1696 ✅ |
| 65536 | **0** | 0 ✅ |
| 8192 | 8192 | 8192 ✅ |

> ⚠ **两个 DMA 表现完全一致**，且裸 `MMIO` 与 `Pynq.dma.read()` 读值相同
> （已排除"地址混叠""驱动封装"两种可能）。

### 3.3 修复

```tcl
CONFIG.c_sg_length_width  {24} \    # 24 位 = 16 MB 上限
```

两个 DMA 都设。重新生成比特流后，`bd_video.hwh` 里确认：

```
dma_in    C_SG_LENGTH_WIDTH = 24
dma_out   C_SG_LENGTH_WIDTH = 24
```

**修复前后对比**：

| | 修复前 | 修复后 |
|---|---|---|
| 启动回读 LENGTH | `8192 (写 614400) ⚠ 不符` | **`614400 OK`** |
| 跑一帧 | 10 秒超时 | **0.005 s 完成** |
| 输出 | 全 0 | **2658/9216 非零** |
| 汇总 | FAILED | **PASSED (11 项)** |

### 3.4 为什么难查

1. **不报错** —— `IOC_Irq` 照常置位，"传输完成"看起来完全正常。
2. **csim / cosim 查不出来** —— 仿真里没有真实的 AXI DMA 长度寄存器，
   这个限制**只在硬件上存在**。所以 `csim + csynth + cosim 全过`毫无帮助。
3. **`C_SG_LENGTH_WIDTH` 这名字有迷惑性** —— 看起来只跟 Scatter-Gather
   模式有关（我们是 `c_include_sg=0` 的直连模式），容易认为"不用 SG 就不用设"。
   **实际上 Direct 模式也用这个字段存传输长度。**
4. 表现为"跑一帧超时"，而真凶是 BD 里一个从没设过的参数。

---

## 四、故障现象（**排查过程中的三条硬证据**）

这三条是定位根因的关键，按发现顺序列出：

| # | 现象 | 来源 |
|---|---|---|
| **1** | `ap_start` 后 **0.0001 秒** `ap_done` 即置位 | `ap_done_probe.py` |
| **2** | 输出 buffer **9216 字节全 0** | `ap_done_probe.py` |
| **3** | DMA 写 `LENGTH 614400` → 读回 **8192** | `bringup_check.py` 的故障快照 |

> 现象 1+2 一度被误读成"IP 没启动"，实际是**探针没写参数**导致
> HLS 走了合法的提前返回路径（见 §5.3 的错误 #3）。
> **真正的突破口是现象 3** —— 它给出了 `mod 16384` 这条数学规律。


---

## 五、假设与检验（**含已排除项与我自己犯的错**）

> ⚠ 本节记录**诊断脚本自身的 bug**，因为它们一度污染了结论。
> 记录在此以免后续重复踩。

### 5.1 已排除

| 假设 | 排除依据 |
|---|---|
| PL 时钟没起来 | 实测 `FCLK1/2/3 = 100 MHz` |
| HLS 参数检查提前 `return` | 真实场景 `width=640/height=480/roi=320` 均合法 |
| overlay 版本不对 | 基地址与 `.hwh`、手册**逐条吻合** |
| `.bit` 用旧 RTL 综合 | git 显示 HLS 源码上次实质改动「**已回退**」；且重跑比特流复现同一结果 |
| 寄存器偏移写错 | 与 `xgesture_preproc_hw.h` 逐条一致 |
| BD 里 `gesture_preproc` 没连 | `.hwh` 里 `src`/`dst` 接口都在，AXI-Lite 也连了；`.tcl` 里 4 条连接齐全 |
| AXI DMA 地址混叠 | 裸 `MMIO` 与 `Pynq.dma.read()` 读值一致；写 `dma_in` 不影响 `dma_out` |
| IP 没启动 / 没进流水线 | 故障快照显示 `CTRL=0x01`（`ap_start=1`、`ap_idle=0`） |
| `dma_in` 的软复位读错寄存器 | ❌ 是我读错，实际代码传的就是 `S2MM_DMASR`（见 §8 订正） |

### 5.2 最终根因（**已确诊并修复**）

**`C_SG_LENGTH_WIDTH` 默认值 14 位** —— 详见 §三。

诊断路径：`ap_done` 超时 → 故障现场快照 → 发现 `LENGTH` 读回
`8192`（写入 614400）→ 写一组值找规律 → **`读回 = 写入 mod 16384`**
→ 查 `.hwh` 确认 `C_SG_LENGTH_WIDTH = 14` → 修复为 24 → 通过。

### 5.3 ⚠ 我（AI）在诊断过程中犯的错

**记录在案。这一节是本文最有价值的部分之一** ——
它说明为什么这个 bug 花了那么久：**大量时间消耗在诊断工具自身的缺陷上，
而不是问题本身**。

| # | 错误 | 后果 |
|---|---|---|
| 1 | 把 `DMASR` 的 **bit1(IDLE)** 当成完成位 | 误报「IP 没跑」，方向跑偏一轮 |
| 2 | 重写 `dma_diag.py` 时**删了 `import numpy` 却留下依赖它的代码** | `NameError`，白跑一轮 |
| 3 | **`ap_start_probe.py` 忘了写参数**（`width`/`roi` 全 0） | 触发 HLS 的**合法提前返回**（`state1→state8`），误读成「IP 启动失败」，**又跑偏一轮** |
| 4 | 把 `.hwh` 当 JSON 解析（实际是 XML） | 卡住一轮 |
| 5 | `print_info` 读不存在的 `base_addr` 键 | 误报所有地址为 `0x00000000` |
| 6 | `full_run_probe.py` 把 `S2MM_DSTADDR` 写成 `0x30`（应为 `0x48`） | 发现后删除，未造成后果 |
| 7 | 拿 `.bit` **字节数**当版本判据（4,045,692 三次都一样） | `.bit` 是**整片器件镜像**，大小由器件决定，与设计无关 —— 该判据无效 |

> **教训 1**：改脚本时删了 A 却留下依赖 A 的 B，**语法检查查不出来**。
> 后续加脚本必须跑**未定义名检查**（见 §7）。
>
> **教训 2**：**`ap_done` 是 COR 位**，任何「边轮询边判完成」的代码都要当心。
>
> **教训 3**：**探针必须先把被测系统置到"有效状态"再测**。
> 错误 #3 是典型的"测了一个未配置的系统，然后解读它的正常行为为故障"。
>
> **教训 4**：**排除一个假设，要用证据，不能只凭 commit message 或文件大小。**
> 错误 #7 就是用了一个**根本无效的判据**。

---

## 六、遗留 / 降级项

| 项 | 状态 |
|---|---|
| **§2.2 引脚万用表复核** | ⚠ **未做** —— 上电前应做但跳过了。摄像头引脚映射是"镜像"推理的，**插错会烧板** |
| **HDMI 线** | 未插（BD 里无 TMDS 编码器，端口悬空，**不要接**） |
| 摄像头 | 未接（③ 未通过，前置条件不满足） |
| SOC / 摄像头寄存器表 | 未验证 |

---

## 七、复现命令（下次直接照抄）

### PC 侧（Git Bash，提示符 `xiaomu@DESKTOP-...`）

```bash
# 从 XSA 解出 .bit / .hwh（.hwh 必须改名，PYNQ 靠同名配对）
cd /e/complete_project_source/vivado/gesture_system
unzip -o gesture_system.xsa -d /tmp/xsa_extract
scp gesture_system.bit              xilinx@192.168.2.99:/home/xilinx/
scp /tmp/xsa_extract/bd_video.hwh   xilinx@192.168.2.99:/home/xilinx/gesture_system.hwh

# 传脚本
scp /e/complete_project_source/host/bringup_check.py    xilinx@192.168.2.99:/home/xilinx/
scp /e/complete_project_source/host/gesture_overlay.py  xilinx@192.168.2.99:/home/xilinx/
```

### 板子侧（提示符 `xilinx@pynq`）

```bash
# ⚠ 三个要素缺一不可：sudo + -E + 解释器全路径
sudo -E /usr/local/share/pynq-venv/bin/python3 bringup_check.py --bit /home/xilinx/gesture_system.bit
```

**判定**：`*** BRINGUP CHECK PASSED ***`（⚠ 注意 §8 的假成功 bug）

### 新增的诊断脚本（在 `host/`）

| 脚本 | 作用 |
|---|---|
| `ap_done_probe.py` | 只测「IP 跑没跑」「DMA 搬没搬」，**不依赖对状态位的解读** |
| `dma_diag.py` | DMA 状态逐步打印（⚠ 版本 1/2 均有 bug，结论已作废） |
| `reg_ident.py` | 寄存器映射探针，**尚未跑** |

### 静态检查（**加脚本后必做**，防 §5.3 的错）

```bash
python -c "
import ast, builtins
src=open('host/新脚本.py',encoding='utf-8').read(); tree=ast.parse(src)
bound=set(dir(builtins))
for n in ast.walk(tree):
    if isinstance(n,ast.Import):
        for a in n.names: bound.add((a.asname or a.name).split('.')[0])
    elif isinstance(n,ast.ImportFrom):
        for a in n.names: bound.add(a.asname or a.name)
    elif isinstance(n,(ast.FunctionDef,ast.ClassDef)):
        bound.add(n.name)
        if isinstance(n,ast.FunctionDef):
            for a in n.args.args+n.args.kwonlyargs+n.args.posonlyargs: bound.add(a.arg)
            if n.args.vararg: bound.add(n.args.vararg.arg)
            if n.args.kwarg: bound.add(n.args.kwarg.arg)
    elif isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store): bound.add(n.id)
    elif isinstance(n,ast.ExceptHandler) and n.name: bound.add(n.name)
used={n.id for n in ast.walk(tree) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load)}
miss=sorted(used-bound)
print('未定义名:', miss if miss else '无')
"
```

---

## 八、⚠ 本次发现的**代码 bug**（与当前故障未必相关，但都要修）

| # | 位置 | 问题 | 严重性 |
|---|---|---|---|
| 1 | `bringup_check.py` 失败路径（156/168/188/204 行） | **③ FAIL 却打出 `BRINGUP CHECK PASSED`** —— 失败路径用 `print("[FAIL]")` 而非 `_c()`/`_fail()`，`_failed` 没加。**已修** | 🔴 **高**（会把失败伪装成通过，比报错危险） |
| 2 | `gesture_overlay.py:217` | `Overlay()` **无参调用**，此版 PYNQ 要求显式传路径 | 🟡 中 |
| 3 | `gesture_overlay.py:print_info` | 读 `info['base_addr']`，此版 PYNQ 的键是 `phys_addr` → **所有地址显示为 0** | 🟢 低（纯显示） |
| 4 | `board-bringup-guide.md:319` | 「卡在 **FSBL**」表述错误（PYNQ 走 SD 启动，无 FSBL banner） | 🟡 中（**已误导本次排查**） |

> ⚠ **订正**：本文早先版本把 `_soft_reset_dma` 对 `dma_out` 传 `MM2S_DMASR`
> 列为 bug —— **这是我自己读错了**。实际代码（`gesture_overlay.py:431`）
> 传的是 `DMA_S2MM_DMASR`，**本来就是对的**（`dma_in` 用 MM2S、
> `dma_out` 用 S2MM，各按角色）。
> 我的错误源于只看了 `run_once` 里的一段片段就下结论，没看完整调用点。
> 借此把该函数改成**候选列表**形式，作为防御性加固（正确时行为不变）。

---

## 九、下次接着做的建议顺序

1. **先修 §8 的 #1（假成功）** —— 不修的话后续每次失败都可能被伪装成通过
2. **做 §6 的引脚万用表复核** —— 上电前的安全项，跳过有烧板风险
3. **验证假设 C/D** —— 直接读两个 DMA 的 `0x04` 与 `0x34`，看哪个是活的
4. **验证假设 B** —— 改用**项目自己的 C 驱动启动序列**（只写 `0x1`）复测
5. 若以上均无果 → **回到 Vivado 重新生成比特流**（排除一切版本因素）

---

## 附：本次实测的环境参数

| 项 | 值 |
|---|---|
| 板卡 | PYNQ-Z2 (XC7Z020-1CLG400C) |
| PYNQ 镜像 | `pynq_z2_v3.0.1.img`（7,858,807,808 B） |
| Python | 3.10.4 @ `/usr/local/share/pynq-venv/bin/python3` |
| pynq | 3.0.1 |
| PC 网卡 | `以太网 2` = USB2.0 Ethernet Adapter, **100 Mbps** |
| PC IP | `192.168.2.100/24`（手动） |
| 板子 IP | `192.168.2.99` |
| `.bit` | 4,045,692 B（**修复后重新生成**，2026-09-21 21:02） |
| `.hwh` | 744,025 B（同名 `gesture_system.hwh`，**`C_SG_LENGTH_WIDTH = 24`**） |
| PL 时钟 | FCLK0=62.5 / FCLK1=100 / FCLK2=100 / FCLK3=100 MHz |
| **③ 实测耗时** | **0.005 s**（614400 B @100 MHz ≈ 3.1 ms 流水线 + 开销，吻合） |

> ⚠ `.bit` **文件大小恒为 4,045,692 B** —— 它是整片器件的配置镜像，
> 大小由器件（7z020）决定，**与设计内容无关**。
> 判断"比特流是否变了"要用 **MD5**，不能用文件大小。
