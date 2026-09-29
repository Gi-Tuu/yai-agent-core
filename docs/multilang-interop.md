# 多语言接入设计（Multilang Interop）

> 事实基线：yai-agent-core **v0.7.0**，最后核实 **2026-09-29**。

## 0. 先回答：能实现，还是已经实现？

| 能力 | 状态 | 说明 |
|---|---|---|
| **跨语言接入**（接任意语言写的服务） | **✅ 已实现** | 经 **MCP Client** 与 **OpenAPI Discovery**，两者语言中立，有离线测试、线上在用（线上接 DeepWiki MCP） |
| Python **同进程零配置内省** | **✅ 已实现** | `discover(host)` 用 `inspect` 自动生成 ToolSpec，**仅 Python** |
| `ToolSpec` 中立数据 + `registry.register()` | **✅ 已实现** | 工具能力是数据，不绑定语言 |
| **ToolSpec ↔ manifest JSON 序列化 + 文件 IO** | **✅ 已实现** | `to_manifest_dict` / `from_manifest_dict` + `tools/manifest.py`；composite / code 可完整移植并执行，native 类只导出声明 |
| 非 Python 的**同进程零配置内省** | ❌ 不追求对等 | 静态语言无同等运行时元数据，需"注解 / 生成"一步（语言特性，非缺陷） |
| 各语言注解 / 代码生成器（JVM APT、Rust proc-macro、`go:generate`、TS 提取） | 🛠 未做（路线） | 见 §4 |
| 每语言 native exporter 探针 | 🛠 未做（路线） | 见 §4 |
| gRPC Proto / GraphQL / LSP 发现适配器 | 🛠 未做（路线） | 当前只有 MCP / OpenAPI |
| 跨语言一致性测试套件（conformance） | 🛠 未做（愿景） | 见 §5 |
| 内核多语言重写 / FFI bindings | 🚫 不做 | 用 conformance 契约替代，见 §5 |

> 一句话：**"跨语言调用别的服务的工具"现在就能做（MCP/OpenAPI）；"在别的语言里同进程零配置内省"没做、也不追求对等，靠注解 / 探针 / 标准协议接入。**

---

## 1. 核心思想：Core 依赖中立 manifest，不依赖某种语言的反射

自动发现的本质 = **拿到一份机器可读的能力清单（manifest，即 `ToolSpec` / JSON Schema）**。
Python `inspect` 只是"生成清单"的**其中一个 provider**：

```
                       AgentCore（内省 → 缺口感知 → 发现/组合）
                                        │
                    统一加载 ToolSpec（语言中立 manifest）
      ┌──────────────┬──────────────┬──────────────┬──────────────┐
 Python 内省     注解/代码生成    标准协议转换      手写 manifest
 inspect(✅)    JVM APT/proc     OpenAPI(✅)      JSON/YAML 兜底
                macro/generate   MCP(✅)
```

---

## 2. Provider 矩阵：语言 → manifest 来源

| 语言 / 运行时 | 最顺手的清单来源 | 同进程? | 现状 |
|---|---|---|---|
| Python | `inspect` 自动内省 | 同进程、零配置 | ✅ |
| JVM（Java/Kotlin） | 注解 + APT 生成 / 反射 | 同进程需 JVM 内核；通用走 MCP/OpenAPI | MCP/OpenAPI ✅；APT 🛠 |
| TS / JS / Node | TS 编译器 API / 装饰器 | sidecar MCP 最省事 | MCP/OpenAPI ✅；装饰器 🛠 |
| Go | struct tag + `go:generate` | 编译期生成 | MCP/OpenAPI ✅；generate 🛠 |
| Rust | proc-macro / build script | 编译期生成 | MCP/OpenAPI ✅；macro 🛠 |
| C# | Source Generator / 反射 | 编译期 / 反射 | MCP/OpenAPI ✅ |
| **任意 REST/RPC 服务** | **MCP / OpenAPI**（未来 Proto/GraphQL） | **进程外、天然跨语言** | ✅ |

---

## 3. 现在就能用的跨语言接法（✅ 已实现）

### 3.1 经 MCP（让对方语言起一个 MCP Server）

- Core 作为 **MCP Client**，支持 **stdio 子进程**与 **Streamable HTTP** 两种传输；
- 任何语言的 MCP Server 暴露的工具都会被注册进 `ToolRegistry`；
- 配置（见 `.env.example`）：`MCP_SERVER_URL=<http形态>` 或 `MCP_SERVER_COMMAND=<stdio形态>`。

### 3.2 经 OpenAPI（让对方语言的 HTTP 服务产出 OpenAPI 描述）

- 给一个 OpenAPI spec（在线 URL 或本地文件，JSON/YAML），自动把端点注册为工具；
- 配置：`OPENAPI_SPEC_URL` / `OPENAPI_SPEC_PATH`，公网演示可用 `OPENAPI_READ_ONLY=1` 只注册 GET/HEAD。

> 这两条路**不要求对方是 Python**，是当前已验证的跨语言接入方式。

---

## 4. 还没做、如何演进（路线，按性价比排序）

1. **每语言 manifest 生成器**：JVM 注解处理器（APT）、Rust proc-macro、Go `go:generate`、TS 编译器提取——编译期产出 `ToolSpec` JSON。
2. **native exporter 探针**：每语言一个极小程序，在宿主侧反射后经 stdio/HTTP 吐出 manifest JSON；Core 只认 JSON、不关心语言。
3. **更多标准协议适配器**：gRPC（Proto）、GraphQL、LSP（JSON-RPC）——与 MCP/OpenAPI 同层。

> 内核的 Python 代码不需要为这些改动；它们都是新增的 **manifest provider**。

> **边界：不另造"manifest + 自有 JSON-RPC 调用通道"**——那等于一个简化版 MCP。
> manifest 只负责"能力的语言中立描述与交换"；跨语言的实际调用统一走 MCP / OpenAPI。

---

## 5. Conformance 愿景：不重写内核，也能让多语言"同源"

与其用 Java/Rust 重写内核、维护 FFI bindings（一个人不可行），不如发布三样**语言中立**的东西：

1. **manifest schema**（`ToolSpec` 的 JSON 序列化，**已具备**：`to_manifest_dict` + `tools/manifest.py`）；
2. **行为契约**（内省 → 缺口感知 → 发现/组合的语义）；
3. **跨语言一致性测试套件（conformance suite）**。

各语言的 YAI 实现可以**各自生长、不共享代码**，只要通过同一套 conformance 测试即视为兼容。
这让"多语言支持"由社区 / 需求驱动，内核作者无需背多语言维护成本。

---

## 6. 诚实边界

- **同进程、零配置内省是动态语言（Python）的红利**；静态语言天然多一步"注解 / 生成"。
- 目标是"**每种语言都能以该生态的最低摩擦接入**"，不是"所有语言都和 Python 一样零配置"。
- 真正可移植的核心是**自适应的心智模型与契约**，不要求是 Python 代码本身。

---

## 附：相关文件

| 文件 | 作用 |
|---|---|
| `src/yai_core/integrations/mcp/client.py` | MCP Client（stdio + HTTP） |
| `src/yai_core/integrations/openapi/{discovery,spec,client}.py` | OpenAPI 发现（JSON/YAML） |
| `src/yai_core/tools/manifest.py` | manifest 批量导入导出 + 文件 IO |
| `src/yai_core/discovery/` | Python 函数内省 |
| `src/yai_core/spi/` | 8 个能力契约 |
| `docs/walkthrough/10-mcp-client.md` / `12-openapi-discovery.md` | 逐行讲义 |
