# ScreenCast、GPU 与 CPU 连接验证

2026-10-22，Fedora 44 KDE/KOS Wayland 宿主，Debian 13 原生消费端。开发分支 `codex/screencast-protocol`，基线 `0c14564`。腾讯会议修复已由用户实测确认通过，本轮接续通用协议连接。

## 本轮结果

| 实验 | 实际结果 | 记录 |
|---|---|---|
| Portal 窗口选择与 FD 传递 | 实际 v5；CreateSession → SelectSources → Start → OpenPipeWireRemote；经过现有过滤代理 | 三份 capture.json 的 portal/phases |
| CPU MemFd | 60 帧，BGRx，900×598，色块匹配，57 次哈希变化 | [MemFd](work/capture-memfd-165141/capture.json) |
| GPU DMA-BUF | 60 帧，BGRx，900×598，linear modifier，EGL/GLES 导入成功，56 次哈希变化 | [DMA-BUF](work/capture-dmabuf-175846/capture.json) |
| GPU 失败后自动回退 | 不存在的 render node 明确失败；关闭原 FD，重新 OpenPipeWireRemote；MemFd 取得 60 帧，48 次哈希变化 | [auto](work/capture-auto-180017/capture.json) |
| 宿主 GBM 自检 | 自建 127×61 XRGB8888 linear 缓冲；导出 DMA-BUF、EGL 导入、RGB 7747/7747 匹配 | [宿主记录](gpu-host-roundtrip.json) |
| Debian GBM 自检 | 相同原生流程，RGB 7747/7747 匹配 | [Debian 记录](gpu-debian13-roundtrip.json) |
| 代码回归 | 102 项，98 通过，4 项既有现场测试按原规则跳过 | [测试结果](regression.json)、[日志](regression.log) |

GPU 为 Mesa Intel Iris Xe (TGL GT2)。宿主自检使用 Mesa 26.2.4，Debian 自检使用 Mesa 25.0.7-2+deb13u1。GPU 验证包含 `glReadPixels` CPU 读回，因此全部结果明确 `zero_copy=false`，不作为零拷贝或性能结论。

系统选择由用户操作，每次只选择 `Multilinux Screen Share Test` 合成窗口。窗口显示红、绿、蓝色块与变化的计数器。证据保留格式、尺寸、颜色计数、帧哈希、状态和错误，不保存图像字节。

## 实现与集成修复

- `src/multilinux/screencast.py`：延迟加载 Gio，先订阅 Response 再发请求，按真实返回 handle 匹配；UnixFDList handle 按索引取实际 FD；取消、超时与关闭释放 Request/Session/FD；支持 v5 node ID 与 v6 serial。
- `tools/probe_screencast.py`：将受限 FD 传给有界消费进程，解析 NDJSON，严格区分 GPU 成功、CPU 读回和回退；每次尝试取得新 FD；失败也保存元数据。
- `fixtures/screencast_consumer.c`：只通过继承 FD 连接 PipeWire，实际处理 MemFd 或 single-plane DMA-BUF；检查 CPU offset/stride/chunk，GPU 使用 EGLImage/texture/FBO 导入；每条路径均回收缓冲。
- `fixtures/dmabuf_roundtrip.c`：独立私有 GBM 缓冲导出/导入/逐像素自检。
- `tools/probe_desktop_protocols.py`：独立实验环境、原生编译、客户端启动预检、合成窗口和取帧编排；每次捕获保存源码快照、哈希和构建来源。
- `src/multilinux/on_demand.py`：长中文实验路径超过 AF_UNIX 路径长度限制；代理改在私有目录中以相对名称 `bus` 绑定。访问规则保持原样，真实 Portal 流已验证穿过代理。

父代理在现场集成中发现并修复 Gio `a{sv}` 外层 tuple 构造问题；真实 Gio 编码检查和实际会话均通过。

按 [PipeWire 1.4 DMA-BUF 协议](https://docs.pipewire.org/1.4/page_dma_buf.html)，EGL 导入允许 `maxsize`/`chunk.size` 为 0，不用线性内存大小判断 tiled 缓冲。modifier 以 mandatory/don't-fixate 协商，过滤 external-only 与不能证明是单 plane 的组合。MemFd 的内存边界检查保留。

协议入口另见 [ScreenCast Portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html) 和 [PipeWire Portal](https://docs.pipewire.org/page_portal.html)。本机实际接口为 v5；v6 serial 仅由注入测试覆盖。

## 失败与复测

首次准备把实验项目父目录用作共享路径，被已有隔离检查拒绝，记录留在相邻 `screencast-2026-10-22T155040+0800/manifest.json`。随后改为独立 `project/` 和 `work/`。

`capture-memfd-163729` 和 `capture-memfd-164410` 都失败于 `pw_context_new`：Debian 缺少 `client.conf`。仅装开发库、common 和 modules 不足；通过原生包查询确认文件由 `pipewire-bin` 提供，补装后先创建 PipeWire context 预检，再进行现场复测。包事务分别留在 `dependencies.json`、`runtime-dependencies.json`、`runtime-client-config-dependencies.json`。失败没有计入成功帧。

CPU 首次成功使用协议修正前的消费端，其对应源码已独立保存在该捕获目录。GPU 与 auto 使用最终协议修正版本，auto 同时复验了最终源码的 CPU 路径；各自 `provenance.json` 和 `source/` 可核对。

## 回收与范围

最终独立 `screen-debian13` 环境停止，启动租约为空、bridge 为 null，合成窗口退出；三次成功流程均关闭 Portal session 和全部 remote FD。宿主 2522 项 RPM 包的清单哈希与桌面入口名单前后相同，见 [postconditions.json](postconditions.json)。原腾讯会议部署保留。

[validation.json](validation.json) 对现场帧、GPU 导入、回退、源码来源、回收、宿主清单和 DSH 模型通道作机器核验。`SHA256SUMS` 可从仓库根目录用 `sha256sum -c` 检查，编译产物和本机状态不纳入提交。

本轮验收仅覆盖这台宿主、Debian 消费端、合成窗口、single-plane packed RGB 和实际协商到的 linear modifier。非线性 modifier、多 plane、其它 GPU、monitor 选择、长时间采集、长期预览/编码界面与跨宿主 M5 仍待后续验证。

## 复现与接手

环境快照为本目录 `project/`，共享工作目录为 `work/`；容器保留但已停止。新机器先用新证据目录执行 `prepare`；此机可直接 `build` 后捕获：

```bash
python3 tools/probe_desktop_protocols.py build --run-dir evidence/screencast-2026-10-22T155700+0800
python3 tools/probe_desktop_protocols.py capture --run-dir evidence/screencast-2026-10-22T155700+0800 --mode dmabuf
python3 tools/probe_desktop_protocols.py capture --run-dir evidence/screencast-2026-10-22T155700+0800 --mode auto --render-node /nonexistent/multilinux-renderD999
```

本轮三名外部子代理均通过 DSH、Command Code 套餐的 `command-code / deepseek/deepseek-v4.1-flash` 执行，未切到官方 DeepSeek provider。会话入口、完成状态和交接报告在 [agents/](agents/delegation.json)；不提交凭据或原始推理日志。项目总接手入口为根目录 `HANDOFF.md`。
