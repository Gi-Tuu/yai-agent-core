# 逐行讲解 35 · 生产加固 P1：预算熔断、落盘加密、CoreConfig、SQLite 热备份

> 对应文件：
> - 预算：`src/yai_core/kernel/budget.py`（新文件，零依赖）、`kernel/loop.py`
>   （统一计数与熔断）、`types.py`（`BUDGET_EXCEEDED` 事件、`TokenUsage`）、
>   `llm/openai_compat.py`（从 completion.usage 取真实 token）；
> - 加密：`src/yai_core/spi/cipher.py`（第九个 SPI）、`memory/vault.py`（透明加解密）、
>   `integrations/crypto/fernet_cipher.py`（`[crypto]` extra 的 Fernet 电池）；
> - 配置：`src/yai_core/config.py`（新文件，零依赖 `CoreConfig`）、`core.py`
>   （早期校验、`_resolve_router`、`from_config`）；
> - 备份：`src/yai_core/memory/sqlite_store.py`（`backup_to` / `restore_to`）；
> - 离线测试：`tests/test_budget.py`、`tests/test_storage_cipher.py`、
>   `tests/test_core_config.py`、`tests/test_sqlite_backup.py`（全部零网络、零 API Key）。
>
> 读前先读第 6（loop）、8（core）、11（SQLite）、28（长期记忆）、34（P0 三件套）。
> P0 解决"挂着不炸"，P1 解决四件**花钱与丢数据**相关的事：**别失控烧钱、别明文落盘、
> 配置别传错、库文件能热备能恢复。**

## A. 四项加固一览

| 加固 | 防的事故 | 一句话做法 | 红线 |
|---|---|---|---|
| ⑥ 预算熔断 | 模型/工具被异常循环狂调，账单失控（平台不充值也怕被刷） | 任务级三个硬上限，超限立即终止并发事件 | 零依赖，只在真实 usage 存在时计 token |
| ④ 落盘加密 | 长期记忆（可能含 PII）以明文 JSON 躺在磁盘 | 加 `StorageCipher` SPI，vault 透明加解密 | 内核不内置算法，Fernet 进可选 extra |
| ⑤ CoreConfig | 二十多个 kwargs 难复用、传错值要跑到中途才炸 | 配置对象集中默认值 + 构造早期 `validate()` | 公共签名不变，纯增量 |
| ⑦ SQLite 热备份 | 要备份就得停服；崩溃后不知数据还在不在 | 标准库在线 `Connection.backup` + 恢复类方法 | 只用标准库，持锁保证一致快照 |

共同纪律与 P0 一致：**内核零第三方硬依赖、默认行为与旧版等价、全部离线可测。**

---

## B. ⑥ 预算熔断：三个硬上限，超限即停

### B.1 数据模型（`kernel/budget.py` 全文很短）

```python
@dataclass(frozen=True)
class Budget:
    max_model_calls: int | None = None   # 本任务模型调用上限（不含路由分类/压缩）
    max_tool_calls: int | None = None    # 普通工具调用上限（组合工具对外算 1 次）
    max_tokens: int | None = None        # 累计 token 上限（仅在模型回传 usage 时计）
    def __post_init__(self): ...         # 拒绝负数；bool 不算 int

class BudgetExceeded(RuntimeError):
    def __init__(self, scope: str, used: int, limit: int): ...

class BudgetTracker:
    def __init__(self, budget: Budget | None): ...
    def record_model_call(self): ...     # 超限抛 BudgetExceeded(SCOPE_MODEL_CALLS)
    def record_tool_call(self): ...
    def record_tokens(self, usage: TokenUsage): ...
    def snapshot(self) -> dict: ...      # 给 DONE 事件做账本回执
```

三个刻意的口径：

1. **`None` = 不限制**，所以不传 `Budget` 时行为与旧版**完全一致**；`0` = 一次都不允许
   （可用于"只许走规则、不许调模型"的极端宿主）。
2. **模型计数点在统一入口 `_model_chat`，但排除路由 `aclassify` 与压缩**。路由分类是内核
   自己的小动作、压缩是兜底维护，都不该消耗用户的"任务模型预算"；否则预算口径会虚高。
3. **工具计数点在 `_execute_tool_calls` 的普通工具分支**。meta-tool（`request_capability`/
   `create_code_tool`）是控制信号不是业务动作，组合工具内部编排 N 步对用户只呈现为一次
   工具调用，所以都不重复计数。
4. **token 只在 `ModelResponse.usage` 真实存在时累计**。很多兼容端点不回 usage，这时
   `max_tokens` 不生效而不是瞎估——宁可不管，也不编数字。

### B.2 熔断怎么接到主循环

- `astream` 在**顶层（depth==0）**建一个 `BudgetTracker`；澄清递归（depth>0）复用同一个，
  这样"追问两轮"不会把预算清零重来。
- 在通用 `except Exception` **之前**加 `except BudgetExceeded`：发一条固定的模型话术
  `MODEL_MESSAGE` 和一个 `BUDGET_EXCEEDED` 事件（data 带 scope/used/limit/snapshot），
  然后**立即终止**，DONE 的 reason 为 `"budget_exceeded"`。
- 两处"吞异常回退"（react 局部、模型兜底）都补了 `except BudgetExceeded: raise`，
  保证熔断信号不会被"降级容错"误吞——**预算超限是硬终止，不是可降级的小毛病。**

### B.3 token 从哪来

`types.py` 新增 `TokenUsage(prompt_tokens, completion_tokens, total_tokens)` 并挂到
`ModelResponse.usage: TokenUsage | None`；`openai_compat.py` 在 completion 带 `usage` 时
构造它，不带就留 `None`。ScriptedModel 测试可以手动塞 usage 来确定性触发 token 熔断。

---

## C. ④ 落盘加密：第九个 SPI，内核只定契约

### C.1 为什么是 SPI 而不是直接内置 cryptography

内核本体 `dependencies=[]` 是硬红线。加密算法（AWS/Fernet/国密）选型和密钥管理强依赖
宿主环境（桌面端密钥链、服务端 KMS、HSM），内核不该替宿主决定。所以：

```python
# spi/cipher.py
@runtime_checkable
class StorageCipher(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> bytes: ...   # 失败抛异常
```

同步、字节进字节出——vault 的落盘本来就是同步小 IO，不必引入异步复杂度。

### C.2 vault 的透明加解密与"解密失败=冷启动"

`MemoryVault(path, cipher=...)`：

- `_save`：`text = json.dumps(payload)`；有 cipher 就 `tmp.write_bytes(cipher.encrypt(text.encode()))`，
  没有就 `write_text` 明文；最后 `os.replace` **原子替换**（半截写盘不会损坏旧文件）。
- `_load`：有 cipher 走 `read_bytes() → cipher.decrypt() → decode`。解密链捕获三类问题：
  文件不存在、常规 JSON/编码错误，以及**最宽的 `except Exception`（解密失败/密钥不符/被篡改）**，
  一律视为"这份密文我读不出来"，**冷启动为空库并重写**，绝不让进程起不来。

> 安全取舍：默认不传 cipher 仍是明文 JSON（向后兼容、本地 demo 友好），docstring 明确写了
> "不传 cipher = 落盘明文"。要静态加密就显式传，生产公网主机应当传。

### C.3 Fernet 电池（`[crypto]` extra）

`integrations/crypto/fernet_cipher.py` 懒加载 `cryptography`：

- `FernetStorageCipher(key=None)` 生成一次性密钥（测试用），传 str 自动 encode；
- `FernetStorageCipher.generate_key()` 静态生成可持久化的 key；
- `FernetStorageCipher.from_env("YAI_VAULT_KEY")` 从环境变量取 key，**缺变量直接 RuntimeError**
  （而不是悄悄生成一把重启就变的 key，那会让数据每次重启都解不开）。

安装 `pip install -e '.[crypto]'` 即得 AES-128-CBC + HMAC 的认证加密；篡改密文会抛
`InvalidToken`，正好被 vault 的冷启动分支接住。

---

## D. ⑤ CoreConfig：把二十多个开关收敛成一个可校验对象

### D.1 配置对象长什么样

`config.py` 是零依赖 dataclass，字段与 `AgentCore.__init__` 的可选项一一对应、默认值相同；
核心是两个方法：

```python
def validate(self) -> CoreConfig:
    # max_iters 必须 >=1；max_clarify_rounds/full_schema_budget >=0
    # bool 一律不算 int（True 不是合法的"次数"）
    # llm_router ∈ {True, False, "auto"}
    # context_compaction ∈ {bool, CompactionPolicy}
    # budget 必须是 Budget 或 None；model_timeout 必须是正数或 None
    ...
def as_kwargs(self) -> dict:
    return dataclasses.asdict(self)
```

### D.2 三种用法等价，错误提前到装配期

```python
# 1) 旧写法完全保留（签名、默认值不变）
core = AgentCore(model, max_iters=6, llm_router="auto")

# 2) 一份配置多处复用（多专员/多子员工共享同一套预算与开关）
cfg = CoreConfig(budget=Budget(max_model_calls=20), context_compaction=True)
core = AgentCore.from_config(model, cfg)

# 3) 非法值在构造当下就抛 ValueError，而不是跑到任务中途才炸
AgentCore(model, max_iters=0)        # 立即 ValueError
```

`AgentCore.__init__` 体开头先用全部入参构造一个 `CoreConfig(...).validate()`，所以无论走
哪种写法都逃不过早期校验。

### D.3 顺手抽离的 `_resolve_router`

原来 `__init__` 里内联了二十行"显式 router 优先 / 否则按 llm_router 决定 route_model /
挂学习器"的分支，现抽成 `@staticmethod _resolve_router(...)`：输入输出纯粹、可独立阅读，
`__init__` 缩短为一行调用。这是克制的重构——**只搬边界清晰的纯函数，不去重排 loop 心脏。**

---

## E. ⑦ SQLite 热备份：不停服也能拿到一致快照

### E.1 为什么用 `Connection.backup` 而不是复制文件

WAL 模式下直接 `cp` 数据库文件可能漏掉还在 `-wal` 里的页，得到一份不一致的副本；停服
复制又违背"在线"诉求。Python 标准库的 `sqlite3.Connection.backup` 做**页级在线拷贝**，
备份期间数据库可继续读，写被短暂串行（小库毫秒级）：

`backup_to` 的顺序是：

1. 持 `self._lock`，先检查连接未关闭，再由 `PRAGMA database_list` 取得实际源路径。
2. 拒绝源=目标及路径/大小写/符号链接/硬链接别名，也保护源的 `-wal/-shm/-journal`。
   目标不得是符号链接或有硬链接别名；目标存在任何侧车时直接拒绝，不猜它是否陈旧。
3. 用 `mkstemp` 在**目标同目录**独占创建暂存库，再执行 `Connection.backup`。
4. 暂存库切到 `journal_mode=DELETE`（backup 会继承源的 WAL 模式），执行
   `integrity_check` 并检查非空 schema，关闭连接，确认暂存无侧车。
5. 再检查一次目标及别名/侧车，最后一次 `os.replace` 发布完整的单文件库。
   复制、校验、连接或替换失败均保留旧目标，只清理本次创建的暂存及其侧车。

`:memory:` 库也能 `backup_to` 成文件。**在线指源在线，不是允许覆盖在线目标**；
SQLite 在线读取可能维护源 SHM 的 read marks，不要求在线源 SHM 字节不变。

### E.2 恢复、侧车顺序与原子性边界

`restore_to(backup, dest)` 保持离线契约：

- 先解析源路径，拒绝别名/侧车/硬链接源（硬链接可能隐藏另一路径的 WAL），检查文件存在
  及 SQLite 文件头；缺失源抛 `OSError`，空白/损坏源抛 `sqlite3.DatabaseError`。
- 源以 URI `mode=ro&immutable=1` 打开，执行 `integrity_check` 与非空 schema 校验；
  不做 schema 迁移。合法但没有 schema 的空库也拒绝，业务表存在但没有记录则允许。
  `mode=ro` 禁止创建源；单独只读仍可能创建/修改 WAL 的 SHM，故增加 `immutable=1`。
  **immutable 会忽略 WAL，因此恢复源必须已 checkpoint、无侧车且全程不变**。
- 使用与热备份相同的同目录暂存机制，暂存校验通过并关闭源、目标临时连接后，才发布。
  源缺失/空白/损坏以及发布前的失败不会改动源和旧目标，也不会留下本次临时文件
  （前提是操作系统允许清理；强杀或文件系统故障不属于正常异常清理保证）。
- **侧车不参与替换，也从不自动删除**。先删旧 WAL 会丢失未 checkpoint 数据；先换主库
  再删旧 WAL 又可能让旧 WAL 配上新主库。多次 rename 加异常回滚仍不是崩溃原子的
  多文件事务。因此遇到任何 `-wal/-shm/-journal`（包括空文件）直接失败，原样保留。
- 正常流程：停止访问源/目标的服务并关闭所有连接 → 必要时由运维在独占维护窗口使用
  SQLite 完成恢复/checkpoint、正常关闭 → 确认无侧车 → `restore_to` → 重新打开目标。
  不要用手工删除 WAL 的方式绕过检查；若只有一份带 WAL 的备份，先对副本做维护。
- **边界未消失**：侧车检查不是跨进程互斥锁，检测不到所有无侧车的空闲连接，也不能消除
  最后检查与 replace 之间的并发打开/换路径竞态。宿主必须保证整个操作期间目标离线，
  恢复源也保持离线不变；这不是可安全覆盖任意正在使用数据库的 API。
- `os.replace` 仅提供同文件系统单文件名称切换的原子性，不承诺 WAL 多文件原子恢复、
  断电持久性或目录 fsync。close 后重开测试只验证正常关闭的数据持久化，**不等于模拟
  了崩溃或断电**；进程强杀在发布前可能留下可人工清理的 `.yai-snapshot-*` 暂存文件。

---

## F. 离线测试怎么把这四件事测实

- **预算**（`test_budget.py`，10 例）：ScriptedModel 预设响应，分别把模型/工具/token 三个
  上限打到超限，断言抛 `BudgetExceeded`、发 `BUDGET_EXCEEDED`、DONE 带账本与 reason；
  再断言不传预算时行为不变、DONE 不带 snapshot。
- **加密**（`test_storage_cipher.py`）：用标准库 base64 写一个可逆 `FakeCipher` 只验证接线
  （磁盘上无明文、且不是裸 JSON）；`BrokenCipher` 验证解密失败冷启动为空不崩；装了
  `cryptography` 时再跑真实 Fernet 的 round-trip、篡改报错、`from_env` 成功/缺变量报错。
- **配置**（`test_core_config.py`）：参数化喂 10 组非法值断言早抛；合法 Budget/Policy/
  `"auto"` 通过；`from_config` 构造的 core 能正常跑一个直答任务且 `max_iters` 透传到 loop。
- **备份**（`test_sqlite_backup.py`，全部使用 `tmp_path`）：文件库正常 close 后重开；
  在线快照含 WAL 中的数据；内存库可导出；源缺失/空白/损坏/无 schema 时文件不变；
  关闭后备份不毁旧目标；复制、暂存打开/校验、发布故障保留旧目标并清理私有暂存；
  拒绝自备份、源/目标别名及源侧车目标；在线目标和未知侧车保留；发布前复查侧车；
  Unicode/URI 特殊字符源路径可恢复，成功发布无旧表残留、无侧车。

早期 P1 的覆盖率记录为：`budget.py`/`config.py`/`cipher.py`/`sqlite_store.py` 均 100%，
`fernet_cipher.py` 93%、全仓 96%；这是历史记录，不代表本次修复后的覆盖率。

---

## G. 自检（读完应能回答）

1. 预算为什么不计路由分类和压缩的模型调用？组合工具为什么对外只算一次工具调用？
2. 模型不回 `usage` 时 `max_tokens` 应该怎样？为什么不能估算？
3. 熔断为什么要在通用 `except Exception` 之前单独捕获，且两处回退里要 `raise`？
4. 为什么加密做成 SPI 而不是把 cryptography 写进内核依赖？
5. vault 解密失败时为什么选择"冷启动为空库"而不是报错退出？这带来什么风险、由谁兜底？
6. `FernetStorageCipher.from_env` 缺环境变量时为什么要直接报错？
7. `CoreConfig.validate()` 为什么把 `bool` 排除在"整数次数"之外？
8. 热备份为什么要持 `self._lock`？为什么不能先删目标的 `-wal/-shm`，而要拒绝带侧车目标？
9. 缺失/空白/损坏备份为什么必须在替换目标前报错？为什么只读恢复还需要 `immutable` 和离线前提？
10. 同目录单文件 replace 能保证什么，为什么它不能保证 WAL 多文件原子性或识别所有在线连接？
