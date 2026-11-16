# CPU 探针与三轮 ISA 回执独立审查

2026-11-15。本审查只读源码、保存的 ELF/反汇编、原始 stdout/stderr、result、receipt、采样与事件日志；没有执行二进制、编译、测试或 SSH。以下 PASS 是对已存在执行证据的独立核对，不是另跑一轮。

## 结论与优先级

未发现本次 ISA 探针、三轮 ISA 执行 harness 或新原子/上下文 fixture 源码需要阻断的问题。三轮 ISA 的 PASS 可成立，范围限于以下基线二进制实际覆盖的已知答案指令、执行前置检查和主客体一致性。不是整个 ISA 的完整合规测试，不涵盖性能、完整 AES 加解密、全部 AVX-512 子扩展或 C03–C05 的全部验收。随后落盘的 context controller 发现一项 P2 失败分类问题，见文末，不影响已保存的 ISA 正向结果。

新 `cpu_atomic.c` / `cpu_context.py` 此处完成静态审查，并在构建回执到达后补核了库哈希、动态段与真实 lock add 指令；本回执没有将新的上下文实机结果计作通过。其后续运行不能借用本节 ISA 的 PASS。

## 已知答案与实际 opcode

源码 `fixtures/hardware/cpu_known_answers.c` SHA256：`d8a573cf17bad85a6f20b36455bce7cb5428ebee495ff336bf074dbed1ee9ff3`。

已执行二进制 SHA256：`f0ca9fa3d5e85e7a7af6228714160fec214759b602d23b42f1ad1c37eabc9ce9`，11,224 bytes。当前文件哈希与三轮 report/spec 记录相同；ELF 记录为 x86-64 EXEC、无 INTERP、无动态段、栈无执行标志。构建采用 `-march=x86-64 -mtune=generic`，没有 `-march=native`。

| 源码位置 | 独立参考核对 | 已存反汇编中的实际指令 |
|---|---|---|
| 34–42 | `123456789`、初值/终值 XOR `ffffffff` 的 CRC-32C 为 `e3069283`；反汇编等价比较未终值 XOR 的 `1cf96d7c` | `0x4001f0 crc32 eax,BYTE PTR [rdx]`，9 字节循环 |
| 44–85 | 两组各 8 路浮点/整数结果均逐项相等；整数输入没有溢出 | `0x400218 vaddps ymm0,...`、`0x400268 vpaddd ymm0,...`，随后写内存并循环比较 |
| 88–104 | `left=1+2^-23`，`right=1-2^-23`，加 `-1`，融合精确值 `-2^-46`；浮点可精确表示，非融合先舍入乘积会得到 0 | `0x4002c0 vfmadd231ss xmm1,xmm0,[right]`，实际结果存储后与参考比较 |
| 106–135 | 输入 00..0f 的 S 盒输出按 ShiftRows 取序 `00,05,0a,0f,04,09,0e,03,08,0d,02,07,0c,01,06,0b`，再逐字节 XOR 5a，得到 `39313d2ca85bf1216a8d2d9fa4263571` | `0x400300 aesenclast xmm0,xmm1`，16 字节循环比较；只声明末轮指令验证 |
| 137–153 | `~0x0f0f0f0f & 0x33cc55aa = 0x30c050a0`；mask `0x292` 的位点为 1/4/7/9，`0xb` 散入后为位 1/4/9，即 `0x212` | `0x40035a andn eax,eax,edx`、`0x40037a pdep eax,eax,edx`，随后直接比较实际寄存器 |
| 155–181 | 16 路整数加法参考逐项正确 | `0x400390 vmovdqu32 zmm0,...`、`0x40039a vpaddd zmm0,...`，实际写回后比较 16 路 |

这些路径使用 `noinline` 和 `asm volatile`，输出约束承接实际寄存器/内存结果；反汇编确认指令与结果比较均保留。因此不能解释为编译器将常量答案直接折叠成 PASS。

`cpu_known_answers.c:254–275` 仅当 CPUID XSAVE 与 OSXSAVE 同时置位才执行 XGETBV；AVX/FMA/AVX2 还要求 AVX 位与 XCR0 的 SSE/YMM 位 `0x6`；AVX-512F 再要求其 CPUID 位及 XCR0 `0xe0`。没有在 gate 前执行 ISA 专用函数。x86-64 基线和初始化栈满足本轮 Linux LP64 两端范围。

## 原始三轮回执

| 回执 | 原生与客体实际结果 | XCR0 | 最低宿主可用内存 | 汇总峰值内存 / tasks | 最高采集 CPU 温度 |
|---|---|---|---:|---:|---:|
| `cpu-local-debian` | 8 项 PASS，含 AVX-512F | `0x2e7` | 3,017,629,696 B | 155,152,384 B / 27 | 62°C |
| `server/cpu-server-fedora44` | 7 项 PASS；AVX-512F `cpuid=false, enabled=false, unsupported` | `0x7` | 115,162,275,840 B | 148,234,240 B / 31 | 30°C |
| `server/cpu-server-arch` | 同上 | `0x7` | 115,099,975,680 B | 106,999,808 B / 31 | 31°C |

三轮均已逐项核对：

- 原始 host/guest stdout 逐字相等；与 `result.json` 中 parsed 值完全一致；两侧 returncode 都是 0，stderr 为空。
- `receipt.reason=null`、command exit 0、没有 `abort.json`。全部保存样本中汇总组和 controller 的 OOM/max/pids 事件计数都是 0；本机 PSI 最高 some 0.84/full 0.65，服务器均 0。
- `formal_stop=STOPPED`，cleanup 与 rescue 显示相同目标 CID `Running=false, Pid=0, OOMKilled=false`；事件日志仅出现各自目标 CID，没有 create 或非目标实例事件。
- worker 最终 `MainPID=0, ControlGroup=""`。虽然 systemd `ActiveState=failed`、容器 sleeper 退出 143，但源码 `reuse_test_guard.py:599–618,663–669` 明确由监督器停止等待中的 worker，SIGTERM handler 返回 143；这是 harness 正常回收形态，不能当成已知答案探针失败。探针的原始退出仍为 0。
- CPU controller `checked_info():31–44` 已明确校验 image ID 与 manifest digest；`check_binary():80–89,251–258,285–289` 校验原生及共享副本哈希；因此不是仅写一个未经核对的 fingerprint 字段。

E5 的 AVX-512F 结果证明缺失扩展被安全拒绝，不证明 E5 执行了 AVX-512。主机与容器耗时口径不同：客体采集包含 Podman/runtime 开销；这些微小样本不能作为 CPU 容器性能损耗结论。温度为短探针前后检查，不应拿来代替未来持续 CPU/GPU 负载的连续温控。

## 新 C03–C05 源码审查

审查快照：

- `cpu_atomic.c` SHA256 `0dd183cbf9bffa9e8c4028c02490ba22a97d5abb4009c522309e406de02dac25`。
- `cpu_context.py` SHA256 `d0427d56bc97fed24952c951077d121cce505a6eb89194854b74ef9fc8278014`。

`cpu_atomic.c:8–25` 静态要求 64 位原子 lock-free，两个线程各固定 10,000 次 relaxed fetch-add；计数器访问均为原子，初始化在启动线程前，最终读取在 join 后。这个场景仅需无丢失更新，不用 relaxed 来宣称跨对象内存排序。构建后已补核 `libcpu_atomic.so` 为 9,752 bytes，SHA256 `c8c20d20e59fa3e747293e6f029f34d1a087cb67e8e707090ffa5fcc4f4aad89`，动态段无 NEEDED；保存反汇编中的 add_fixed 从计数 0x2710 开始、每轮两次真实 `lock add QWORD PTR [counter],1` 并减 2，正好执行 10,000 次，不是常量折叠。T1 构建回执 reason null、command exit 0。

`cpu_context.py:260–278` 以有界大小和 SHA256 验证库，随后通过同一已打开 FD `ctypes.CDLL` 加载；CDLL 调用释放 GIL，工作发生在原生 C 中。`281–330` 固定两工作线程、barrier、共同 2 秒截止和 join；超时用本进程 `os._exit(2)` 回收所有本轮线程，避免 Python 非 daemon 线程阻止退出。最终期望 20,000，检查同步索引及单调时序。它验证这次两线程计数与同步，不保证两个 CPU 核同时重叠执行，也不冒充压力测试。

`89–186` 定位当前可见 cgroup v2，并确认当前 PID 位于该 leaf；只读取可见祖先链。隐藏祖先或缺失文件不会被猜成无限额；C05 明确固定为 PARTIAL 且 `forced_throttling=NOT_RUN`。`202–248` 区分 CPU 可见数量、实际 affinity、cpuset 与 NUMA 节点，没有用 `os.cpu_count()` 冒充 CPU 配额。`251–257` 只读取 perf 权限条件，PMU 为 NOT_RUN，没有声称实际打开事件成功。

汇总时必须注意 `emit():337–346` 的约定：C03 PASS 且 C04 不为 FAIL 就可能返回 0，即使 C04/C05 为 PARTIAL。因此 controller 应解析并保留各 case 的状态，结合保护器实际 cgroup/收尾证据评价 C05，**不能用退出码 0 将 C03–C07 一律登记 PASS**。这是源码刻意保留的结论边界，未发现当前代码把 PARTIAL 伪装成 PASS。

目前无须为本次 ISA 证据重复执行已通过的三轮。新原子库/context 仍需按原计划完成受保护实机验收后，才能扩大 C03–C05 结论。

## 新 context controller 的失败分类（P2）

首次审查的 `cpu-context-controller.py:14–16,89–90` 在读取 JSON 前将所有非零退出视为 RuntimeError，外层保留初始 `status=INVALID`。实际可达情况是原生探针完整输出 `c03.status=FAIL`（计数不符）或 `c04.status=FAIL`（拓扑/亲和性不一致）并按契约返回 1，此时会被错记 INVALID，parsed 也不会写入 result，虽原始 stdout 仍保留。

建议先解析和保存有效 JSON，把契约完整且报告正确性失败的情形标为 FAIL；超时、启动错误、输出不完整或证据格式错误才标 INVALID。这不影响新 controller 的正向 PASS，也不构成安全启动阻断，但应在用它报告失败用例前修正。主控已收到具体位置和建议。当前 controller 对 C04 的 PARTIAL 保留及 C05/C07 不扩张结论的约定可接受。

## 主控闭环
P2 分类问题已修复，原控制器保存在 cpu-context-controller-before-classification.py。context-classification-t1 在独立保护下退出0：复现旧脚本错误分类，验证有效答案失败转 FAIL 且保留 parsed，信号/损坏JSON仍 INVALID，正确答案伴随部分元数据仍保持分项 PARTIAL。本机 context-local-debian 因60秒内无法满足含全部新增预算的内存准入而 NO_START，未执行探针，不归为产品失败、不重试。
