# 逐行讲解 46 · ResourceProvider：让 Agent 能读写文件，但只在一个目录里

> 第 10 个 SPI。前面九个契约管的是"模型/记忆/权限/沙箱"这类能力，这一个管**文件与资源**。
> 拆成两半放：**契约与路径策略在内核**（`spi/resources.py`，纯函数、零 IO），
> **真正碰磁盘的实现在电池层**（`batteries/filesystem.py`）。
> 内核不会因为多了这个契约就获得读盘权限——这是"能力声明"与"能力落地"的分界。

## 0. 为什么不是"给内核加个 file_read 工具"

内置文件工具是最省事的写法，但会把三件事一次做坏：内核默认能读整台机器的文件、
宿主换不掉它、安全策略散落在每个宿主里各写一遍。所以这里反过来：

```
宿主/电池层                        内核
ResourceProvider 协议      ←   spi/resources.py（协议 + 纯路径策略）
FileSystemResources        ←   batteries/filesystem.py（原子写、限额、冲突检测）
file_list/read/create/patch ←  四个 async 工具函数，交给 AgentCore.auto 注册
```

## 1. 路径策略是纯函数，而且**拒绝**而不是"清洗后放行"

`normalize_relative()` 只管形状，非法形状一律 `PathEscapeError`：

| 被拒的形状 | 为什么 |
|---|---|
| `../` 任一段、`notes/../../x` | 上跳就是越权 |
| `/etc/passwd`、`\\server\share`、`C:\Windows` | 绝对路径与 UNC 直接暴露 root 之外 |
| 含 `:` | 既是盘符写法，也是 NTFS 交替数据流（`file.txt:stream`） |
| `~/.ssh/id_rsa` | 用户目录展开等于换个根逃逸 |
| `CON` / `nul.txt` | Windows 保留设备名，POSIX 上"看起来没事"到 Windows 就出事 |
| 段尾点号或空格 | Windows 会静默剥离，校验的路径和落盘的路径就不是同一个 |
| 含 NUL / 控制字符、超深（>16 段）、超长（>512） | 防御性上限 |
| 混用 `\` 与 `/` | 两种分隔符混写时，人和解析器看到的层级不一样 |

**为什么不"把 `..` 悄悄剥掉继续读"**：清洗式做法等于告诉模型"越界会被温和纠正"，
而真实后果是它读到了别人的文件。报错回灌，模型才会改路径。

`resolve_within_root()` 是第二道闸，两道都不可省：

1. **逐段拦链接**：从 root 往下走，路径上每个已存在的段都不能是符号链接或重解析点。
   POSIX 有 `O_NOFOLLOW` 兜底，**Windows 没有**（实测 `hasattr(os, "O_NOFOLLOW")` 为 False），
   所以这一道必须自己走，还要额外看 `st_file_attributes` 的重解析位（junction 也算）；
2. **归属复核**：`realpath` 之后必须仍在 `root.resolve()` 之下。root 自己可能是链接
   （macOS 的 `/tmp` → `/private/tmp`），所以两边都要解析后再比。

第 1 道拦"链到外面"，第 2 道拦"绕出去"。测试里 `allow_symlinks=True` 的宽松策略
**仍然**拦不住逃逸，就是专门钉第 2 道闸独立有效。

两道闸都发生在 `open` 之前，所以还剩"校验完、打开前"的窗口。POSIX 的 `O_NOFOLLOW`
只能保护**最后一段**，Windows 根本没有这个标志；父目录被换成链接时两者都看不见。
电池层因此补了**打开后复核**：读路径用句柄的真实路径（Linux `/proc/self/fd`、
macOS `F_GETPATH`、Windows `GetFinalPathNameByHandleW`）比对校验过的落点；写路径在
`replace`/`link` 前再 `realpath` 一次。平台取不到句柄路径时按"未验证"降级，不假装
已经验证过。

## 2. 写盘的四条实现口径

```
读：存在？是目录？超过 max_read_bytes？→ 是 UTF-8 吗？→ 内容 + sha256
新建：tmp 写入 → fsync → os.link(tmp, target)   # 目标存在即失败，原子
改写：比对 expected_sha256 → tmp 写入 → fsync → os.replace(tmp, target)
失败：任何异常 → 删掉 tmp，原文件从未被就地改写
```

- **原子替换**：`os.replace` 同目录 rename 是原子的；读者永远只会看到旧版或新版，看不到半截。
- **新建用硬链接而不是"先 exists 再 rename"**：后者有 TOCTOU 窗口。少数文件系统不支持
  `os.link`，回退分支里仍然"存在即报冲突"，只是窗口变宽——这条回退路径有专门测试。
- **限额报错而不是截断**：截断后的文件内容看起来是完整的，模型会拿半份内容做判断。
- **进程内一把写锁 + 跨进程靠指纹**：`expected_sha256` 不匹配就是别人先改过 → 冲突。
  跨进程严格互斥需要文件锁，而文件锁在 Windows/NFS 语义不一致，**所以这里明说不承诺**。

## 3. 指纹是给模型用的，不是给人看的

`file_read` 返回 `sha256`，`file_patch` 必须带 `expected_sha256`。这不是形式主义：
它把"我看过这一版"变成机器可校验的事实。模型拿到冲突错误的正确反应是**重读再改**，
而不是重试硬写——这条写在工具的 docstring 里，因为 docstring 就是模型看到的说明书。

## 4. 工具层：四个 async 闭包，零胶水

`filesystem_capabilities(root)` 返回 `file_list / file_read / file_create / file_patch`，
带类型注解和 docstring，直接丢给 `AgentCore.auto` 或 `build_spec` 就能注册。
它们全是 async：真正的读写在 `asyncio.to_thread` 里，不卡事件循环——
桌面端（第 22/23 篇）和在线 API（第 09 篇）共用同一个内核循环，这点必须成立。

返回值只给**相对路径**，绝不回绝对路径：绝对路径本身就是越权信息（暴露用户名、目录结构）。

## 5. 根目录之内还要分块：`filesystem_gate`

两道路径闸管的是"**出不出根目录**"。但宿主真正想说的往往是更窄的一句话：
"这个 Agent 只能碰 `notes/`，别的一律不行"。这件事**电池层管不了**——
它只知道根在哪，不知道哪一块是给谁的。

而把它交给"按原始字符串匹配白名单"也是假的：`notes/a.txt`、`./notes/a.txt`、
`notes//a.txt`、`notes/./a.txt` 是**同一个目标的四种写法**，模型完全可能写出其中
任何一种；更糟的是人工审批时看到的就是这个原始串。

`filesystem_gate(prefixes=("notes/",), write=True)` 做三件事：

```python
from yai_core.batteries.filesystem import filesystem_capabilities, filesystem_gate
from yai_core.types import ToolGovernance

for fn in filesystem_capabilities(root):
    spec = build_spec(fn)
    if fn.__name__ in ("file_create", "file_patch"):        # 写类工具
        spec.governance = ToolGovernance(
            gate=filesystem_gate(prefixes=("notes/",), write=True)
        )
    else:                                                   # 读类工具
        spec.governance = ToolGovernance(
            gate=filesystem_gate(prefixes=("notes/",))
        )
```

1. **用与 handler 完全相同的 `normalize_relative` 规范化**——"批准的目标 == 执行的
   目标"是构造上成立，不是靠约定；四种写法都归一到 `notes/a.txt`；
2. **按前缀白名单判作用域**；`write=True` 而 `prefixes` 为空 = **拒绝一切写**
   （失败关闭，不默认放行）；非法形状（`..`、绝对路径、盘符冒号、空、非字符串）
   在闸口就拒，并把理由回灌给模型；
3. **把规范化后的落点放进 `GateDecision.target`**，于是审批话术与 `TOOL_CALL`
   审计事件里看到的是真落点（第 44 篇的 risk/scope/target 同一条路）。

三条别误会：

- 前缀匹配是**字面**的。Windows/macOS 大小写不敏感、8.3 短名这类"同一文件多个名字"
  不在这里解决——`NOTES/a.txt` 在 `notes/` 白名单下会被**误拒**。这是有意的：
  宁可误拒不可误放，要放宽得宿主自己显式规范化；
- 它**不替代**两道路径闸。符号链接仍在**打开时**由 handler 拦，所以"批准与写入之间
  被换成链接"这种竞态是 fail closed，不是"闸看住了"；
- 它**不做授权**。能不能调用 `file_patch` 仍由 `PermissionPolicy` 决定，闸只能抬不能放
  （`test_gate_cannot_override_host_denial`）。

还有一条反"假绿"的对照测试值得单独说：`test_without_the_gate_the_same_write_succeeds`
——**不挂闸时同一次越界写会成功落盘**。它证明"作用域"这件事确实是闸在管，
而不是电池层碰巧拦住了；写测试时如果只挂一个写工具，这类缺口就会假绿。

## 6. 权限与边界

- 契约本身**不做授权**：能不能写、要不要问人，仍走第 43/44 篇的权限闸与能力闸。
  宿主完全可以把 `file_patch` 挂成"必须人工确认"，或给它配 `ToolGovernance`。
- `read_only=True` 挂载：读与列举照常，写一律拒（给"只给看不给改"的场景）。
- 只支持 UTF-8 文本。二进制文件明确拒绝，不做"先读进来再说"。
- 跨租户靠**每租户一个 provider 实例 + 各自的根目录**（与第 35 篇的多租户口径一致），
  不是靠路径黑名单。

## 7. 自检（读完本篇应能回答）

1. 为什么路径策略要放内核、实现要放电池层？反过来会怎样？
2. Windows 上没有 `O_NOFOLLOW`，靠什么挡住符号链接逃逸？为什么还要第二道归属复核？
3. `file_create` 撞已存在文件、`file_patch` 指纹不匹配，分别报什么？为什么不能都"覆盖算了"？
4. 写失败之后，磁盘上应该留下什么？测试是怎么证明的？
5. "只有 `notes/` 能写"为什么不能靠匹配模型给的原始字符串实现？`filesystem_gate`
   凭什么保证"批准目标 == 执行目标"？
6. 为什么前缀匹配故意做成大小写敏感的？代价是什么？
