# 逐行讲解 24 · 真机自适应全开：路由学习 × 沙箱造工具（`learning_vault.py` + `host.py`）

> 前面几篇把零件都造好了：第 19 篇讲自校准路由（bandit）的原理，
> 第 18 篇讲代码工具与 TTL，第 22、23 篇讲原生桌面壳和多专员灵动岛。
> 但在很长一段时间里，这些能力**没有在评委亲手玩的那个产品里同时打开**——
> bandit 只在 examples / benchmark 里跑，灵动岛主打产品的"越用越准"反而是关的。
> 本篇把它们在真机灵动岛里全部接通，并以**仓库专员为自适应全开样板间**。

## 0. 要解决的核心矛盾：Core 每任务一销毁，学习却要跨任务累积

灵动岛的装配原则是"**每任务一个真 Core**"：用户发一句话，`factory` 里
`AgentCore.auto(...)` 现场建一个 Core，跑完 `astream`，这个 Core 就被丢弃。
这样做的好处是上下文、状态天然干净，任务之间不串味。

但"自适应"恰恰要求**跨任务**：

- 上一类任务用 react 成功率高，下次同类任务应更倾向 react（bandit 学习）；
- 上一个任务在沙箱里造的工具，下次还想用（代码工具持久化）。

这两类"会学习的状态"不能随 Core 一起死。解法是把它们放到 **Core 之外**：
内存里放进 `factory` 的**闭包**，磁盘上写进 `data/desktop/` 的 JSON 文件。

## 1. 先分清两种"跨任务记忆"，它们极易混淆

灵动岛每个 live 专员名下有**两类**文件，名字像、职责完全不同：

| | 代码工具仓库 | 路由学习状态 |
|---|---|---|
| 文件 | `*_code_tools.json` | `*_route_learning.json` |
| 记什么 | 沙箱造的 / 组合出来的**工具** | 每种上下文该选哪个**策略** |
| 生命周期 | 48h TTL，被调用就刷新 | 长期累积，不自动过期 |
| 内核侧能力 | `sandbox` + `composition` | `learning`（bandit 选择器） |
| 面板入口 | 工具管理面板 | 学习状态（`summarize`） |

记忆法：**code_tools 记"手"（学会的动作），route_learning 记"脑"（遇事走哪条路）。**

## 2. `learning_vault.py` 逐行：为什么它故意不依赖 Qt

这个模块负责路由学习状态的读写与摘要。文件开头第一句注释就点明它
"**不依赖 Qt**"——这是关键设计：

```python
from yai_core import ContextualBanditSelector
```

它只 import 内核与标准库（`json` / `pathlib`），不碰 PySide6。
于是 CI 的**精简矩阵**（没有 Qt、只跑内核测试的那一组）也能直接测它，
不必为了测一个读写函数去启动 GUI。

### 2.1 固定种子与中文标签

```python
LEARNING_SEED = 42
_LENGTH_LABELS = {"short": "短任务", ...}
_STRATEGY_LABELS = {"direct": "直接回答", "react": "工具推理",
                    "plan": "先规划", "clarify": "先澄清"}
```

- 固定种子让演示**可复现**；bandit 的探索随机性仍由 Beta 后验自然提供，
  不需要额外随机。
- 几组 `_LABELS` 是给面板看的"翻译表"：内部用英文枚举，界面显示中文。

`describe_context(parts)` 把第 19 篇那个 9 维上下文桶（任务长度、是否行动、
是否多步、是否含糊、是否疑问、含不含英文、有没有数据对象、工具多寡……）
拼成一句中文标签，例如"中等任务·疑问查询·工具少"。

### 2.2 容错读：坏了就冷启动，绝不拖垮启动

```python
def load_selector(path):
    p = Path(path)
    try:
        if p.exists():
            return ContextualBanditSelector.load(p, seed=LEARNING_SEED)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    return ContextualBanditSelector(seed=LEARNING_SEED)
```

读盘只可能"锦上添花"，绝不能"雪中送葬"：文件被人手动改坏、半截写入、
版本对不上，任何一种异常都**回退到一个全新选择器**（纯规则先验），
灵动岛照常启动。这是产品代码和 demo 代码的分水岭——demo 假设 happy path，
产品假设磁盘上什么都可能发生。

### 2.3 容错写：自动建目录，失败不致命

```python
def save_selector(selector, path):
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        selector.save(p)
        return True
    except OSError:
        return False
```

`parents=True, exist_ok=True` 让第一次运行（`data/desktop/` 还不存在）也能写。
返回布尔值是给调用方/测试一个明确信号；写失败只丢这一次学习，不弹错、不中断。

### 2.4 `summarize`：把 Beta 后验算成人话

面板不能直接展示一堆 Beta 分布参数，`_summarize_dict` 把每个上下文桶
（`state` 里一张表）算成可读结构：

```python
means = {arm: (a / (a + b) if (a + b) > 0 else 0.0)
         for arm, (a, b) in table.items()}
preferred = max(means, key=lambda arm: means[arm])
```

- 第 19 篇讲过，每个策略臂维护一个 Beta(α, β) 后验，**α/(α+β) 就是该臂
  "成功率"的期望**；这里把它算成 `mean`。
- `preferred` 是当前均值最高的臂；`confidence` 是它的均值。
- 桶按观测数降序排（`-observed`），最常遇到的场景排最前。

`summarize` 的外壳同样容错：文件缺失/损坏返回
`{"enabled": False, "learned_tasks": 0, ...}`，面板在"从没学过"时也能正常渲染。

### 2.5 学习面板怎么上岛：workbench 派发（与工具仓库同一套）

`summarize` 只是个纯函数，真正把它接到灵动岛的是 `workbench.py`，
路径与第 23 篇 / 工具管理面板**完全同构**：

```python
learningVault = Property(str, ..., notify=learningUpdated)
learningAvailable = Property(bool, ..., notify=stateChanged)

@Slot()
def openLearning(self):
    self._submit_learning(self._learning_refresh)
```

- 标题栏"学习"按钮调 `openLearning()` → `_submit_learning` 把 `summarize(path)`
  排到工作台**唯一那条常驻 loop**（空闲时没有 Core 实例，真实状态只在磁盘 JSON 里）；
- worker 线程读好、`json.dumps` 后经 `learningUpdated` 回传，`LearningPanel.qml`
  再 `JSON.parse` 渲染；
- 面板对用户是**只读**的：Beta 后验只能由任务成败自然更新，不提供手工编辑，
  避免误操作清空"越用越准"的积累。

`LearningPanel.qml` 每个场景桶画四条后验均值条（直接回答 / 工具推理 / 先规划 /
先澄清），偏好臂用绿色高亮——四臂成功率的此消彼长一眼可见。切专员时面板自动
关闭（`boundSid` 变化），免得把上一个专员的学习状态误当成当前专员的。

## 3. `_live_factory` 的三处接线（`host.py`）

`_live_factory(host, read_tools, *, with_sandbox, storage_path, learning_path)`
为一个小宿主生成事件流工厂。前三步的全部秘密都在这个函数里。

### 3.1 闭包：选择器在任务之间共享

```python
shared = {"selector": None}
```

这一行在 `factory` **定义之外**、`_live_factory` **调用之时**执行一次。
Python 闭包会捕获这个字典，于是无论 `factory` 被调用多少次（多少个任务），
它们读写的都是**同一个** `shared["selector"]`。用字典而不是普通变量，是因为
内层函数要对它**重新赋值**（`shared["selector"] = ...`），字典改内容不需要
`nonlocal`。

### 3.2 首个任务时懒加载

```python
if learning_path is not None and shared["selector"] is None:
    shared["selector"] = load_selector(learning_path)
```

两个条件：传了学习路径，且还没加载过。于是选择器在**第一个任务真正开始时**
才从磁盘恢复，而不是装配 specs 时就一次性全读出来；之后的任务直接复用内存里
那个，不再读盘。

### 3.3 装配 Core：把三类能力一起插上

```python
core = AgentCore.auto(
    host, model,
    channel=channel,
    policy=_make_policy(read_tools, channel.permission_mode),
    llm_router="auto",
    composition=True,
    sandbox=sandbox,
    code_storage=storage_path,
    learning=shared["selector"],
)
```

逐个看：

- `sandbox`：`with_sandbox=True` 时才 `SubprocessSandbox()`（懒 import，
  见下），模型可在授权后于沙箱造代码工具；
- `code_storage=storage_path`：代码工具跨任务落盘恢复（48h TTL）；
- `learning=shared["selector"]`：把跨任务共享的 bandit 选择器交给 Core；
- `composition=True`：组合工具（只编排已注册工具）始终可用；
- `llm_router="auto"`：见第 4 节，是"真机智能 / 离线可测"的开关。

沙箱也是懒加载的：

```python
sandbox = None
if with_sandbox:
    from shell.desktop.sandbox import SubprocessSandbox
    sandbox = SubprocessSandbox()
```

把 import 放进 `if`，没接沙箱的专员（以及不需要它的测试）就不会付出
import 成本，也不要求沙箱依赖就位。

### 3.4 任务跑完才回灌学习

```python
async for event in core.astream(task):
    yield event
if shared["selector"] is not None:
    save_selector(shared["selector"], learning_path)
```

`save` 放在 `async for` **正常跑完之后**是有意的：

- Core 在 `astream` 的 `finally` 里，只有任务**正常完成**才把这一轮的
  策略成败回灌给选择器（被用户取消的任务不产生学习信号——一次取消
  不代表策略错了）；
- 如果任务被取消，`async for` 直接抛 `CancelledError`，根本走不到 `save`，
  与内核"取消不学习"的口径一致。

## 4. `llm_router="auto"`：真机才智能，离线仍可测

这是最容易让人困惑的一个参数，三种取值：

| 取值 | 行为 |
|---|---|
| `False` | 永远走确定性规则路由 |
| `True` | 强制走 LLM 分类（不依赖模型标记） |
| `"auto"` | **模型后端自报 `yai_live_router` 标记才走 LLM**，否则回退规则 |

为什么需要 `"auto"`：项目的测试文化是"全部离线、ScriptedModel、无 API Key"。
脚本模型不会自报 `yai_live_router`，所以 `"auto"` 下它**走规则**，
脚本模型只在 react 循环里被调用，不会被当成分类器——测试因此稳定可重复。
而真实模型（DeepSeek / Agnes 等）会自报该标记，真机就用上 LLM 语义路由。
一个参数同时满足了"离线可测"和"真机智能"，不需要为测试和产品维护两套代码。

## 5. 仓库专员：自适应全开样板间

`DemoWarehouse` 是个极简库存宿主（`list_products` / `get_stock` / `restock`），
工具少反而是优点——模型很容易发现它"缺能力"（比如想算库存总价值却没有单价），
从而自然触发"发现缺口 → 授权 → 沙箱造工具 → 用上"。

它的专员装配把所有能力都打开了：

```python
SpecialistSpec(
    id="warehouse", ...
    live_factory=_live_factory(
        DemoWarehouse(), _READ_TOOLS["warehouse"],
        with_sandbox=True,          # 沙箱造工具
        storage_path=wh_storage,    # 工具跨任务持久化
        learning_path=wh_learning,  # 路由越用越准
    ),
    code_storage=wh_storage,        # 工具管理面板入口
    learning_path=wh_learning,
)
```

"全开"= 路由学习 + 沙箱造工具 + 组合工具 + 工具持久化 + 工具管理 +
子员工委派（`attach_delegate(core, depth=1)`）在同一个专员进程里协同。

这个样板间有一份**可执行证明** `test_warehouse_showcase_full_adaptive`：
脚本化跑两个任务，断言第一个任务发出 `strategy_selected / tool_call /
tool_result / done`，第二个任务后 `summarize(learning)["learned_tasks"] == 2`
——"越用越准"不是一句口号，是测试里能数出来的观测数。

## 6. 三专员能力覆盖矩阵

| 专员 | 真机 live | 沙箱造工具 | 路由学习 | 工具持久化/管理 |
|---|---|---|---|---|
| 销售 sales（默认） | 是 | 是 | 是 | 是 |
| 仓库 warehouse（样板间） | 是 | 是 | 是 | 是 |
| 笔记 notes | 是 | 是 | 是 | 是 |
| 陪伴 companion | 仅演示脚本 | — | — | — |

陪伴专员保持演示：陪伴是 AMBRACE 的主场，灵动岛里不重复造一个陪伴宿主。

## 7. 诚实边界（别在材料里说过头）

- **取消不学习**：被取消的任务不回灌策略、不落盘，这是设计不是 bug。
- **写盘失败只丢一次**：`save_selector` 返回 False 时演示照常，学习状态可能
  比实际少一条。
- **沙箱仍是教学级**：Windows 上没有内存 / CPU 的 rlimit，只靠超时杀进程，
  也不做 OS 级网络隔离；生产环境要换 Docker / gVisor / Firecracker
  （见第 18 篇与 host_g）。
- **模型不一定照剧本走**：真机里模型可能选择组合工具、甚至直接手算，
  而不是造新工具——这本身就是自适应决策，不算失败。

## 8. 自检（读完本篇应能回答）

1. 灵动岛"每任务一 Core"，为什么路由学习和代码工具不会随 Core 一起丢？
2. `*_code_tools.json` 和 `*_route_learning.json` 分别记什么？生命周期有何不同？
3. `learning_vault.py` 为什么刻意不 import Qt？这换来什么好处？
4. `shared = {"selector": None}` 为什么写在 `factory` 外面？为什么用字典？
5. 为什么学习状态只在任务"正常跑完"后才落盘？取消时发生什么？
6. `llm_router="auto"` 在脚本模型和真实模型下分别走哪条路？为什么这样设计？
7. 全开样板间"全开"具体包含哪几项能力？哪条测试在为"越用越准"背书？
