此文件为本地模型交接原文。主控独立验收见 report.md 和 qwen-task1-validation.json。

已完成实际改动与运行。报告如下。

## 1. 修复前失败证据（先加测试、后改实现）

先只改 `tests/test_app_entries.py`（新增 2 个测试方法，未删任何现有测试），对**旧实现**（`text.splitlines()` + `line.strip()`）运行 `python3 -m unittest test_app_entries -v`：

- 结果：`Ran 7 tests ... FAILED (failures=17)`，其中 5 个原有测试全部 `ok`（兼容性未破坏）。
- 17 个失败子用例分类：
  - **字节保留类 7 例 FAIL**：`/usr/share/data/app sample \n`、pacman 前缀 `app /usr/share/data/app sample \n`（尾部空格被 strip 删）；`/usr/share/data/app\u00a0`、`app\u3000`（尾部 Unicode 空格被删）；`a\u2028b`、`a\u2029b`、`a\x85` 被 splitlines 当换行拆开、尾段非绝对路径被丢。断言实例：`AssertionError: Lists differ: ['/usr/share/data/a'] != ['/usr/share/data/a\u2028b']`。
  - **控制字符类 10 例 FAIL**（旧代码不报错而伪造/缩短路径）：`\x01`、`\x00`、`\x7f`、行内 `\t`、`\x0b`、`\x0c`、`\x1c`（`/usr/bin/a\x1cb/usr/bin/b` 旧读成两条假路径）、`\x1e`、pacman 前缀 + `\x1d`、CRLF `/usr/bin/app\r\n`。
- 改动前三模块基线（确认新测试是唯一失败源）：`Ran 35 tests ... OK`。

## 2. 改动文件（仅此两个，其他文件只读；未 add/commit/reset/clean）

- `src/unifier/packages.py` — 只重写 `parse_package_files`（现 97–123 行）：`text.split("\n")`（LF 是唯一记录分隔符）；`line.lstrip(" \t")`（只删路径前的排版空白，保留原有“前置格式空格”行为）；pacman `package + " " + 绝对路径` 前缀剥离后**保留剩余全部字节**；仅对确实成为绝对路径的记录检查 ASCII C0(0x00–0x1F)/DEL(0x7F)，命中即 `raise UnifierError("package app file list has a control character in the path record ...")`；无执行、无展开、无 strip 尾字节；docstring 记录边界。git diff 中该文件其余 hunk（`package_command`/`package_environment` 等）是主控既有未暂存工作，我未触碰。
- `tests/test_app_entries.py` — `git diff --numstat` = `45 0`（纯新增）：`test_package_file_listing_splits_on_lf_only_and_keeps_the_record_bytes`（含原始绝对路径、pacman 前缀格式、前置空格、尾部空格/Unicode 空格、U+2028/U+2029/U+0085、shell 元字符 `$ \` {} () ; | & # ? * [] \` 字面保留）与 `test_package_file_listing_refuses_inline_control_characters`（`assertRaisesRegex(UnifierError, "control")`）。

## 3. 测试实际结果

- 指定命令 `env PYTHONDONTWRITEBYTECODE=1 LC_ALL=C LANG=C PYTHONPATH=src:tests python3 -m unittest test_app_entries test_dependencies test_packages -v`：**Ran 37 tests in 0.496s — OK（exit 0）**；35 个原有 + 2 个新增，`test_app_entries` 7 个方法全 ok。
- 额外自愿回归（8 个相关模块 test_cli_shortcuts / test_local_package_paths / test_package_recovery / test_package_alias / test_app_binding / test_app_user / test_quality_regressions / test_release_regressions）：**Ran 82 tests — OK**。
- 额外探针：无结尾 LF、纯空白、非绝对说明行、U+009C（C1，保留）、非 ASCII 名字尾部双空格均按预期；未运行真实 Podman、未联网。

## 4. 未解决限制（请主控验收时注意）

1. **文件名内含 LF 仍不可表达**：LF-only 列表把这类记录读成两条，本函数不声称往返（docstring 已明说）。真正无损需改用 NUL 分隔（`-Ql`/`rpm -ql --queryformat '%{FILENAMES}\0'`/`dpkg-query -f`），属后续工作。
2. **CRLF/PTY 语义收紧**：`\r\n` 现在抛 `UnifierError`，旧代码静默当 LF。若 guest 命令在 PTY 下输出 CRLF，包文件发现会失败而非容忍；本次未运行真实 Podman，未实测 dpkg-query/rpm/pacman 是否有任何 C0/CR 输出。
3. **仅对绝对路径记录报控制字符错**：非绝对行（如 `\x1c/usr/bin/x`、工具告警行）内含 C0/DEL 时仍静默丢弃，与原有“非绝对行忽略”一致但不对称。
4. **前导空格/tab 视为排版**：绝对路径不可能以空格开头，所以删除安全；但这也意味着旧代码对前导 `\v`/`\f`/NBSP 的容错消失（这些行现在按非绝对行丢弃）。
5. **报错时机/文案变化**：`dependencies.py:144` 的 `"ambiguous package file list"` 现在通常先被解析层的更具体错误取代（无测试断言旧文案，`grep` 确认），调用方若匹配旧文案需检查。
6. 索引中（staged）该函数仍是 `line.rsplit(" ", 1)[-1]`（按最后一个空格截断，另一种字节破坏）；我保留工作区形式且未 stage，索引与工作区不一致，是否提交由主控决定。

以上为我本地自测结论，不等于主控验收。