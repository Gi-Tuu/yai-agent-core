# ACC v1 官方向量语料（vendored fixture）

| 项 | 值 |
|---|---|
| 上游仓库 | https://github.com/agent-capability/agent-capability-contract |
| 上游路径 | `conformance/v1/vectors.json` |
| 分支 / commit | `main` / `b5c48f665196ce6fa372cb471ee54f08b0008540` |
| 抓取日期 | 2026-10-13 |
| 规范版本 | specification_release `1.0.5`（corpus format_version 1） |
| 许可证 | Apache-2.0（上游 `LICENSE`），本项目仅以测试 fixture 形式原样复制，未修改任何字节 |
| SHA-256（本文件同目录 `vectors.json`） | `c3d3943362aade6d4bbf5599edd2300895d1b7bee8cedec5604bff127a01db71` |
| SHA-256（`schemas/acc.v1.schema.json`，未复制） | `d4267c26ff68e28db0944cccfa21031102062fd2393ed7a711b5c70e457b7bee` |
| SHA-256（`conformance/v1/vectors.schema.json`，未复制） | `5676db059d35177bc15fce6f706f38566222d1e713216b00ec34d8c27b469622` |

## 这份语料证明什么、不证明什么

上游 `conformance/v1/COVERAGE.md` 的原话：跑通语料 = **实现适配器把向量喂进公开接口后观测到期望结果**，
它不证明真实网关/运行时的派发闸门。向量里的 `scope_allowed` 与 `trusted_subject` 是**抽象输入布尔**，
不是被验证过的身份或网络观测；`allow` 只表示"该条件不产生审批意图"，不表示"最终被授权"。

26 条向量的构成：declaration 5 / exposure 4 / approval 14 / risk-default 2 / audit-hint 1。

## 本地维护约定

- 只在上游发布新的 `1.x` 修订时重新抓取，同步更新本表的 commit 与 SHA-256；
- 语料一旦变化，`tests/test_acc_conformance.py::test_corpus_metadata` 会先失败，提醒改的是版本而不是实现；
- 实现限制（skipped / 更保守的偏离）记录在 `tests/test_acc_conformance.py` 的 `LIMITATIONS` 常量里，
  不写在 fixture 里。
