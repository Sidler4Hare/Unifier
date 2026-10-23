# 复杂 Arch 应用与后续测试

2026-10-22，基于当前代码与 startup-2026-10-22T182400+0800 实测记录。优先级按已暴露的风险和新增覆盖确定，而不是按包体积。

## 优先复验的产品问题

1. **进程与租约恢复**：修复 PID 1 后重复热启动 100 次，要求孤儿进程被回收；同时测试强杀启动器、应用先退出/后退出、重新受管启动、多窗口并发及手动保持运行的环境。上轮实际发现停止超时 8 秒、Arch 155 个僵尸进程和异常后 auto_stop 丢失，这些仍未修复。
2. **快速重开与竞态**：在最后一窗口关闭后的不同时间点启动下一窗口，覆盖停止前、停止中、停止后；检查租约、代理和应用是否匹配。重跑同口径窗口与 buffer 就绪测量，不能只测 CLI 返回。
3. **包事务故障恢复**：破损本地包、缺依赖、版本冲突、下载断开、pacman 锁、维护脚本失败；逐项检查退出码、包数据库、事务状态和用户可见原因。Arch 本地安装前的完整升级可能已改变环境，不能承诺原子回滚。
4. **实际复杂应用工作流**：Qt/Python/C++ 扩展加载、中文空格路径、保存和重新打开、导出并由解析器回读。FreeCAD 的可验证几何量适合作为 oracle。
5. **长时间媒体链路**：OBS 或 Kdenlive 进行预览、编码、音视频同步、连续 30 分钟和源断开重连；现有 60 帧合成窗口验证不能替代长期采集。
6. **会话和设备变化**：登出/登录后 Wayland 和 Xauthority 改变、屏幕缩放与热插拔、音频设备切换、GPU 权限失败。跨 Debian/Arch 宿主的 M5 仍独立待测。

## GitHub 候选

| 应用 | 上游 / 安装来源 | 为什么有测试价值 | 本轮范围 |
|---|---|---|---|
| FreeCAD | [GitHub](https://github.com/FreeCAD/FreeCAD)、[Arch 官方包](https://archlinux.org/packages/extra/x86_64/freecad/) | Qt、Python、Coin3D、OpenCASCADE；建模、参数重算、FCStd 与 STEP | 独立 Arch 环境中的真实 GUI 工作流，结果见当轮报告 |
| Sunshine | [GitHub Release](https://github.com/LizardByte/Sunshine/releases/tag/v2026.914.233613) | 上游直接提供 `.pkg.tar.zst`；udev、capabilities、GPU/输入/音频集成 | 下载哈希、包路由、原生安装与命令入口；串流端到端尚未覆盖 |
| Kdenlive | [GitHub](https://github.com/KDE/kdenlive/blob/master/dev-docs/build.md) | Qt/KDE、MLT、FFmpeg，项目保存与渲染 | 候选，未安装 |
| OBS Studio 相关 Arch 打包 | [CachyOS PKGBUILD](https://github.com/CachyOS/CachyOS-PKGBUILDS/blob/master/obs-studio-browser/PKGBUILD) | 浏览器插件、PipeWire、编码、音频与捕获 | 候选，未安装；需先检查对应发行版和依赖兼容 |

FreeCAD 是 GitHub 上游项目配合 Arch 官方原生包；Sunshine 才是直接取自 GitHub Release 的 Arch 二进制包。两种来源分别记录。安装成功、维护脚本完整执行、GUI 可用和实际功能验收必须分开判断。
