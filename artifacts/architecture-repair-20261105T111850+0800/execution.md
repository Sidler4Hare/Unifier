# 架构修复续批

用户要求继续修复，范围S3/F03与S4/F04，继续保全S1/S2及既有脏改动。未授权部署、提交、新容器或活动桌面。R=1GiB；单作业512MiB/96任务/1CPU/0swap/每批300s，独立64MiB/16任务，250ms监测，30秒准入。真实集成关闭，私有HOME/XDG。
S3设计：复用现有FD3 READY握手和GuestProcess，客体自身Python3的subreaper监督实际后代（含double-fork/setsid），没有Python3或prctl支持则明确拒绝。结束必须有token/PID/start绑定的终态回执，监督者消失不能当结束。租约先保存token/完整环境绑定，身份未知保留，可显式recover。不给客体安装依赖，不复制宿主解释器/库。原同实例auto-stop所有权及项目会话lease并存。
S4有界委派：仅dependencies.py及新测试与摘要；保留JSON旧字段，增加静态证据范围，bundled候选不等于可加载；不运行任何程序或模型/测试。主控独立接入展示及验证。
本轮不运行容器或GUI；T2/PRE03和同根fixture阻断继续保留。T1微型监督者/ELF fixture只在受控宿主私有根验证机制，不能替代客体/GUI验收。所有测试/模型阶段串行，具体保护失败保留回执，不自动重试。
