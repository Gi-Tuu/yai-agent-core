# 逐行讲解 23 · 多专员工作台与三态交互（`shell/desktop/workbench.py` + `qml/`）

> 第 22 篇把薄壳的脸换成了原生桌面端，但那时一座岛只服务一个员工。
> 本篇是它的另一半：**一座岛管理多个彼此隔离的专员**，以及"球 → 胶囊 → 面板"的交互形态机。
> 立场先钉死：**灵动岛是前台/交换机，专员的大脑长在应用里**——
> 它不做规划、不持有跨应用大上下文、不自己决定调用哪个应用，
> 只做四件事：连接各应用内置的 Core、切换当前专员、显示该专员的事件、转发授权/回答。

## 0. 为什么不是"一个聚合所有工具的大 Core"

把四个应用并进一个 Core 确实更省事，但那就变成第 21 篇之前所有框架的样子：
一个需要预先接线的超级 Agent。这里的分工相反——

| | 聚合大 Core（已否决） | 多专员工作台（本方案） |
|---|---|---|
| 工具集 | 一个注册表装下所有应用 | 每专员一套，互不可见 |
| 上下文 | 共享大上下文 | 每专员独立记忆 |
| 权限 | 一处挡位 | 每专员独立挡位 |
| 界面 | 一个 Agent 说话 | 切换身份，谁的事谁报 |

红线：第一版切专员由用户**手动点选**，不做"意图自动分发器"。

## 1. 三层拆分

```
WorkbenchRuntime（1 个，唯一后台 asyncio 线程）
   ├─ SpecialistRuntime "sales"      独立 busy/pending/挡位/未读
   ├─ SpecialistRuntime "warehouse"  └─ live：DemoWarehouse + AgentCore.auto
   ├─ SpecialistRuntime "notes"      └─ live：DemoNotes + AgentCore.auto
   └─ SpecialistRuntime "companion"  仅演示脚本
        ↓ 事件带 sid 汇聚
   specialistEvent(sid, type, payloadJson)   → QML 一份信号入口
```

- **`SpecialistRuntime`**（`specialist.py`）就是第 22 篇的 `ShellRuntime` 去掉线程所有权：
  loop 由工作台注入，**不为每个专员开一条线程**。方法签名不变。
- **`WorkbenchRuntime`**（`workbench.py`）对 QML 只暴露聚合 Property
  （`activeBusy / activePendingKind / activePermissionMode / …`）与转发 Slot，
  界面永远不需要知道"有几个专员"。
- **`SpecialistListModel`** 给切换条供行，roles：`specialistId / name / glyph /
  busy / pendingKind / unread / enabled / active / runMode`。

## 2. 隔离的三条硬证据（都有测试）

`tests/test_desktop_workbench.py`：

1. **忙隔离**：A 起任务后切到 B，`activeBusy` 立刻是 `False`（画面不被打断），
   B 能独立起任务；A 完成只让 `A.unread += 1`。
2. **挂起隔离**：A 卡在授权时切到 B，`activePendingKind == ""`，
   而 `get("a").pending_kind` 仍是 `confirm`——决策跟着专员走，不会串到别人身上。
3. **收尾唯一**：`shutdown()` 终止全部专员，只关那一条共享 loop。

## 3. 交互形态机：球 →（悬停）胶囊 →（点击）面板

`qml/main.qml` 只有一个字符串状态 `shape: "ball" | "capsule" | "panel"`，
窗口尺寸由它派生，**左上角不动**：

```qml
width:  shape === "panel" ? Theme.panelW : (shape === "capsule" ? Theme.capW : Theme.ballSize)
height: shape === "panel" ? Theme.panelH : (shape === "capsule" ? Theme.capH : Theme.ballSize)
// 没有 x/y 绑定：球→胶囊只向右长，胶囊→面板只向下长；移动整岛交给系统
```

- **悬停**：`HoverHandler` 必须挂在**唯一的内容根** `surface`（所有子树的祖先）上。
  挂在背景 `shell` 上是错的——Qt 的 hover 只沿祖先链冒泡，指针一进入兄弟子树
  （胶囊层、面板层）`shell` 就不算被悬停，表现为"指针还没到输入框就收回为球"。
- **驻留与宽限**：球态要驻留 110ms 才展开成胶囊（路过不触发）；
  判定"离开"留 320ms 宽限，避免指针跨子项时瞬时丢 hover 造成抖动。
- **自动收回**：`hovering` 变假 → 回到球；但 `workbench.activePendingKind !== ""`
  时**拒绝收回**——有决策必须被看到。
- **点击 vs 拖动**：按下后位移超过 `Theme.dragSlop`（4px）才判定拖动，
  并交给 `Window.startSystemMove()` 由平台移动窗口（手算 `mouse.screenX`
  位移在多屏/DPI 下容易出错）；没超阈值就是点击展开。面板同样能拖：
  `Panel.qml` 根节点第一个子项是一层背景 MouseArea（z 序在所有控件之下）。
- **圆角**：每态各自的小圆角（球 `height/2`、胶囊 26、面板 28），
  不要写 `Math.min(width, height) / 2` —— 面板态算出 210，形变中间帧会割出扇形/椭圆。

## 4. 面板以对话为主体，过程默认折叠

时间线只有两种行（同一个 ListModel，靠 `fKind` 分流）：

| 行 | 来源事件 | 呈现 |
|---|---|---|
| `say` | `model_message` / `done` / `error` / 用户发问 | 气泡：你说=右侧紫边，它答=左侧深底 |
| `chain` | 路由、工具调用与结果、缺口、发现、组合、子员工 | 一条 `› 过程 · N 步`，点击展开明细 |

`addStep()` 会**并入上一条 chain 行**（同一轮的过程攒在一起），遇到 `say` 就断开新的一轮。
每个专员各存一份 ListModel（`Qt.createQmlObject` 建、`Binding` 按 `activeSpecialistId` 换给
`ListView`），切走再切回历史不丢。feed 只是本次会话的可视日志，**长期历史归 Core memory**，
工作台不另存对话。

## 5. 演示脚本里的授权卡是真的

`shell/demo_script.py` 的脚本走到 `permission_asked` / `clarify_requested` 时
**不再伪造事件**，而是 `await channel.confirm(...)` / `await channel.ask(...)` 真的挂起。
所以无 Key 演示里点的"允许/拒绝"是走完整权限链路的真决策，
点拒绝会真的按"用户拒绝，未执行该操作"收尾。

## 6. 踩过的坑（都留了注释或测试）

- `module "YaiTheme" is not installed` → QML 模块目录名必须与 `qmldir` 的模块名一致。
- `Screen.horizontalCenter` 绑定期取 0 → 定位交给 Python 按 `availableGeometry()` 写锚点。
- Windows 原生控件样式不许改 `background` → `QQuickStyle.setStyle("Basic")`。
- **`workbench.model` 是普通 Python 属性，QML 读不到** → 必须包成
  `Property(QObject, ...)`（用 `QAbstractListModel` 当类型会被 Qt 拒：
  `Failed to add property: Invalid property type "QAbstractListModel*"`）。
- **切专员后旧 chip 仍高亮** → `setActiveSpecialist` 只刷新了新行；
  改为 `refresh_all()`，并加断言"只允许一行 active"。
- Loader 载入的 `Panel.qml` 若不显式绑 `width/height`，不会自动撑满窗口，
  右侧留下一块吃点击的透明死区。
- **ListModel 的角色表由第一条 `append` 决定**：首行是 `chain`（没有 `fText`），
  之后 `say` 行的文字会被**静默丢弃**，界面只剩空气泡。解法是每行写全同一组角色。
- ListModel 里存 JS 数组，读回来不是 JS 数组（`.concat` 直接报错）——改存 JSON 串。
- 内联组件里 `parent.toggled()` 指向的是子项而不是组件根：信号要 `chainRoot.toggled()`。
- 壳层用 95% 半透明（`#F212141A`）时，桌面上开着其他软件的窗口会让对话"看起来是空的"
  （文字透出来）；面板改不透明，气泡再抬一档对比 + 描边。

## 7. 自动化测不到的部分（诚实记录）

QTest 合成事件能走 press/release/click：点击展开、点 chip 切专员、
点"过程"行展开明细都已验证；形态机逻辑用直接设 `hovering` 属性 + 真平台截图核对
（球 / 胶囊 / 面板 / 授权卡 / 澄清卡 / 切专员六种画面）。但**测不到三件事**：
悬停变胶囊、拖动、指针离开自动收回。原因不是代码，是事件源：

- 合成 `MouseMove` 不产生 Qt Quick 的 hover 事件链（`HoverHandler.hovered` 恒为假）；
- 合成事件的 `mouse.screenX/screenY` 是 NaN，且 `startSystemMove()` 需要真实指针抓取。

这三件要用真鼠标过一遍；形态机本身的逻辑已通过直接设 `hovering` 属性验证
（球→胶囊→离开回球三种截图都在）。

## 8. 自检（读完本篇应能回答）

1. 为什么工作台只暴露聚合 Property，而不是让 QML 直接连各专员？
2. 专员 A 卡在授权时切到 B，界面上会发生什么？为什么这样才对？
3. 球态悬停为什么要 110ms 驻留、拖动为什么要 4px 阈值？
4. 有挂起决策时为什么禁止自动收回？
5. 哪三件事必须用真鼠标验收？为什么自动化测不到？
