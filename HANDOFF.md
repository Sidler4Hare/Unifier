# 当前接手入口

2026-10-22，本轮开发分支：`codex/screencast-protocol`，基线 `0c14564`。

用户已确认腾讯会议应用轮实测通过。本轮继续共享桌面前端、GPU DMA-BUF 与 CPU MemFd 的协议连接。M5 跨宿主尚未开始。

## 已有修复和部署

- Gemini 修复记录：`/home/omg/.gemini/antigravity/brain/f1790762-eb63-403a-b01c-1864b54fd49e/walkthrough.md`。
- 已验收的腾讯会议部署：`/home/omg/.local/share/multilinux/apps/wemeet/`；启动配置和新增 PipeWire 包事务在该目录的 `.lab/state/`。
- 原始摄像头失败证据是修复前记录，应保留作历史诊断。
- Grok 会话 `01a16174-992e-7cf3-9b3c-97dc91ea6f23` 完成合并和 `0c14564` 的现场证据提交。

## 本轮 DSH 子代理通道

用户指定使用 Command Code 套餐的 `command-code / deepseek/deepseek-v4.1-flash`。
启动器和调用级配置在 `.lab/agents/screencast-20261022T155040/`，不修改全局模型选择。

| 角色 | 持久会话 | 独占文件 |
|---|---|---|
| portal | `session-5c4dc75e-c116-4624-b564-7b0bf29729c4` | `src/multilinux/screencast.py`、`tools/probe_screencast.py`、`tests/test_screencast.py` |
| native | `session-15edc679-945b-4ffa-b552-fb3f7931dbb7` | `fixtures/screencast_consumer.c`、`tests/test_screencast_native.py` |
| gpu | `session-4a0b93b9-c8df-4975-9aac-21fa7ff4a924` | `fixtures/dmabuf_roundtrip.c`、`tests/test_dmabuf_roundtrip.py` |

每个角色的 `*-run.json` 记录进程、状态和会话 ID，`*-handoff.md` 记录完成后的交接。
代理退出后，可用启动器的 `--session-id` 和 `--prompt-file` 继续该代理。启动器只在进程环境中加载本机 Command Code 凭据；不要打印或提交凭据及原始推理日志。

## 本轮现场环境和复现

独立证据目录：`evidence/screencast-2026-10-22T155700+0800/`。
实验项目在其 `project/`，共享输入输出在其 `work/`，环境 ID 为 `screen-debian13`。
原腾讯会议及其登录状态保留；本轮编译依赖安装到独立 Debian 环境。

入口：

```bash
python3 tools/probe_desktop_protocols.py build --run-dir evidence/screencast-2026-10-22T155700+0800
python3 tools/probe_desktop_protocols.py capture --run-dir evidence/screencast-2026-10-22T155700+0800 --mode memfd
python3 tools/probe_desktop_protocols.py capture --run-dir evidence/screencast-2026-10-22T155700+0800 --mode dmabuf
```

抓帧通过系统 Portal 选择名为 `Multilinux Screen Share Test` 的合成窗口，记录实际缓冲类型、帧变化和色块统计，不保存屏幕图像字节。GPU 自检使用自己创建的 GBM 色块缓冲，独立于屏幕抓取。

## 当前完成状态（2026-10-22 18:01 +08:00 后）

- 三个 DSH 代理均已完成。交接报告的可提交副本在证据目录的 `agents/`；持久会话和本机启动器仍可接续。
- 真实系统窗口选择 → CreateSession/SelectSources/Start → OpenPipeWireRemote → Debian 原生消费端完整通过，经过现有过滤 D-Bus 代理，没有放宽其访问规则。
- MemFd 实际取得 60 帧 BGRx，色块通过，哈希变化 57 次。最终源码的 auto 回退复测另取得 60 帧 MemFd，哈希变化 48 次。
- DMA-BUF 实际取得 60 帧 BGRx，900×598，linear modifier，EGL/GLES 导入成功，哈希变化 56 次。使用 `glReadPixels` 核验，明确 `zero_copy=false`。
- auto 故意使用不存在的 render node，先记录 GPU 失败，再关闭原 FD、重新取得 Portal FD 并通过 MemFd，不把 CPU 成功记作 GPU 成功。
- 主机与 Debian 的私有 GBM 缓冲自检均为 7747/7747 RGB 像素匹配。
- 102 项代码回归，98 项通过，4 项旧现场测试按原规则跳过。新窗口采集由独立现场工具实际完成。
- 实验环境已停止，应用租约为空、桥接代理已撤销、合成窗口退出、Portal session/FD 均关闭。宿主包和桌面入口清单前后相同。

集成时修复了三个实际问题：Gio 的 `a{sv}` 参数必须正确构造外层 tuple；长中文项目路径下代理 socket 必须相对私有目录绑定；Debian 需要 `pipewire-bin` 提供 `client.conf`，仅有开发库和 modules 不够。构建阶段现在先检查客户端运行文件和 context 创建，再允许弹出共享框。

DMA-BUF 按 PipeWire 1.4 协议处理：EGL 导入不把 `maxsize`/`chunk.size` 当作线性内存边界，允许这些字段为 0；modifier 使用 mandatory/don't-fixate 协商，过滤 external-only 与不能证明为单 plane 的组合。MemFd 仍严格检查 offset/stride/chunk。

失败记录原样保留：首次准备因共享父目录被拒绝；前两次现场捕获因 Debian 缺少 `client.conf` 而失败。后续记录明确区分失败和复测通过。

## 后续边界

本轮只验收 Fedora 44 KDE/KOS 宿主上的 Debian 13 消费端、合成窗口和 single-plane packed RGB。实际 negotiated modifier 为 linear；非线性 modifier、多 plane、其它 GPU、monitor 选择、Portal v6 serial、长时间压力与跨宿主 M5 不应按本轮结果宣称通过。Portal v6 目前只有注入测试。

上层长期预览/编码界面仍可在这条已验证的连接上继续实现。原腾讯会议部署与其用户验收结果保留，不重开旧摄像头任务。

最终进度应以本轮 `report.md`、`validation.json`、源码哈希和实际捕获结果为准。编译、离线自检、Portal 会话握手、实际取帧、GPU 导入和跨宿主验证分别记录。
