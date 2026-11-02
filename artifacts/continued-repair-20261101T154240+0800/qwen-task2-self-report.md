此文件为本地模型交接原文。主控独立验收见 report.md 和 qwen-task2-validation.json。

完成。

**改动（仅两个文件）**

1. `tests/podman_test_support.py`
   - 新增可 patch 的证据根常量：`EVIDENCE_ROOT = <repo>/artifacts/integration-runs`（离线测试 patch 到临时目录，不落真实 artifacts），以及 `DEFAULT_EVIDENCE_DIR = "evidence"`。
   - 新增 `preserve_default_evidence(root)`：若 `<root>/evidence` 存在，则 `mkdir(parents)` + `copytree` 到 `EVIDENCE_ROOT/<临时根唯一目录名>/`；`copytree` 天然不覆盖已存在目标；任何 `OSError/shutil.Error` 转成 `RuntimeError("integration evidence could not be preserved …; temporary root retained at …")`，原始临时根保持不动。无默认回执时直接返回 `None`，连目录都不建。
   - `isolated_service` 的 `cleanup` 只在"无残留环境记录"检查通过后、`shutil.rmtree(root)` 之前调用保全；显式外部输出（`UNIFIER_TEST_EVIDENCE`）在 root 之外，逻辑原样不变，无新增产品运行逻辑。

2. `tests/test_integration_policy.py` 新增 3 个离线用例 + `_CleanupSink`（直接驱动 helper cleanup 以断言其异常）：
   - `test_default_evidence_receipt_survives_temp_root_cleanup`：核心回归——默认回执写在 `<root>/evidence/m1-lifecycle.json`，经 `addCleanup`/`doCleanups` 后临时根消失、回执按 `EVIDENCE_ROOT/<root.name>/` 保全且内容一致。
   - `test_preservation_conflict_keeps_temp_root_and_existing_target`：目标已存在 → 报 RuntimeError、旧文件不被覆盖、临时根与原回执保留。
   - `test_explicit_external_evidence_stays_outside_the_preserved_run`：外部显式路径原样保留，清理后不生成保全目录。

**验证（各一次，同一命令）**
- 修前：`Ran 6 tests … FAILED (failures=2)` — ①`AssertionError: False is not true : default receipt was deleted with the temporary root /tmp/unifier-evidence-rchui98m`；②`AssertionError: RuntimeError not raised`。
- 修后：`Ran 6 tests in 0.031s … OK`（6/6 通过）。
- 收尾检查：`/tmp/unifier-evidence-*` 无残留；`artifacts/integration-runs` 未被创建（离线未污染真实 artifacts）；两文件仍为 `??`（未 add/commit），其余工作未动。