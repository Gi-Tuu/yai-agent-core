"""上下文老虎机选择器（零依赖的自校准路由内核）。

设计要点：
- 每个任务上下文（``TaskFeatures.key()``）为每个策略（arm）维护一个 Beta(α, β)，
  奖励越高 α 越大，失败越多 β 越大。
- **Thompson Sampling**：建议时从各 arm 的 Beta 分布采样，取最大者，天然在
  "利用已知好策略"与"探索不确定策略"之间平衡。
- **规则先验**：冷启动时用现有规则路由对该任务的判断注入伪计数，
  因此第一次建议就≈规则，不会瞎选；之后真实反馈逐步覆盖先验。
- **连续奖励**用 fractional update：α += r，β += 1 - r。
- 纯标准库（random/json/pathlib），随机种子可注入，离线确定性回放。
- **硬规则区域不表态**：空任务、宿主无工具时直接返回 None，确定性下限
  （空任务必须澄清、无工具必须纯对话）不可被学习器越过。

不训练神经网络、不引入 embedding，~150 行即可离线复现学习曲线。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yai_core.kernel.router import AdaptiveRouter
from yai_core.learning.features import TaskFeatures
from yai_core.learning.outcomes import RouteOutcome
from yai_core.tools.registry import ToolRegistry
from yai_core.types import Strategy

#: 四个路由策略即四个 arm。
ARMS: tuple[Strategy, ...] = (
    Strategy.DIRECT,
    Strategy.REACT,
    Strategy.PLAN,
    Strategy.CLARIFY,
)

#: Beta 先验基线（Jeffreys 风格的弱先验，α=β=1）。
_BASE_ALPHA = 1.0
_BASE_BETA = 1.0

#: 默认不叠加 ε-greedy：Beta 后验的 Thompson Sampling 本身就提供探索
#: （未被充分验证的 arm 采样分布宽，仍有机会被选中；被证实失败的 arm 也不会
#: 被永久判死，偶发的高采样让它有自愈机会）。benchmark 显示纯 Thompson 收敛
#: 最好；epsilon 仅作为可选的"安全网"保留（离线测试/特殊宿主可显式开启）。
_DEFAULT_EPSILON = 0.0
_DEFAULT_MIN_SAMPLES = 4.0
#: 规则先验强度：冷启动时给规则 arm 加 2 个伪成功，使第一次建议≈规则
#: （规则 arm α=3 vs 其余 α=1，明显占优但不锁死），又能被真实反馈较快覆盖。
_DEFAULT_PRIOR_STRENGTH = 2.0
_STATE_VERSION = 1

#: 非上下文模式（消融用）下所有任务共享的固定上下文键。
_GLOBAL_KEY = ("__global__",)


@dataclass(frozen=True)
class RouteSuggestion:
    """学习器的一次路由建议。"""

    strategy: Strategy
    source: str          # bandit:thompson / bandit:explore
    confidence: float    # 选中 arm 当前的 Beta 均值 α/(α+β)，越大越"确信"


class ContextualBanditSelector:
    """按上下文维护 Beta 后验的 Thompson Sampling 选择器。"""

    def __init__(
        self,
        *,
        seed: int | None = None,
        epsilon: float = _DEFAULT_EPSILON,
        min_samples: float = _DEFAULT_MIN_SAMPLES,
        prior_strength: float = _DEFAULT_PRIOR_STRENGTH,
        router: AdaptiveRouter | None = None,
        contextual: bool = True,
    ) -> None:
        self.rng = random.Random(seed)
        self.epsilon = epsilon
        self.min_samples = min_samples
        self.prior_strength = prior_strength
        # contextual=False 时所有任务共享一个上下文（退化为普通 4-arm bandit），
        # 仅供 benchmark 消融实验验证"上下文特征"的价值，产品路径恒为 True。
        self.contextual = contextual
        # 规则先验分类器：默认即纯规则 AdaptiveRouter（无 LLM、零网络）。
        self.router = router if router is not None else AdaptiveRouter()
        # state[key][arm_value] = [alpha, beta]
        self._state: dict[tuple, dict[str, list[float]]] = {}
        # 每个上下文的真实观测条数（不含先验伪计数）。
        self._obs: dict[tuple, int] = {}

    # ------------------------------------------------------------------ 建议

    def suggest(
        self, task: str, registry: ToolRegistry
    ) -> RouteSuggestion | None:
        """返回建议策略；硬规则区域或证据不足时返回 None（交回规则/LLM）。"""
        features = TaskFeatures.from_task(task, registry)
        # 硬规则区域：学习器不表态，确定性下限不可越过。
        if features.length_bucket == "empty" or features.tools_bucket == "none":
            return None

        key = self._key(features)
        table = self._state.get(key)
        if table is None:
            rule = self.router.classify(features.text, registry)
            table = self._seed(rule)
            self._state[key] = table

        total = sum(a + b for a, b in table.values())
        if total < self.min_samples:
            return None

        observed = self._obs.get(key, 0)
        if self.rng.random() < self.epsilon:
            arm = self.rng.choice(ARMS)
            source = "bandit:explore"
        elif observed == 0:
            # 冷启动：该上下文还没有任何真实反馈，只有规则先验伪计数。
            # 此时若直接 Thompson 采样，弱先验（α=3 vs 其余 α=1）下约有一半概率
            # 随机蒙到 plan/clarify，把本该查工具的任务带偏。改为确定性地取先验
            # 最强臂（即规则判断），等第一条真实回灌后再进入 Thompson 采样。
            arm = max(
                ARMS,
                key=lambda s: table[s.value][0] / (table[s.value][0] + table[s.value][1]),
            )
            source = "bandit:thompson"
        else:
            arm = max(ARMS, key=lambda s: self.rng.betavariate(*table[s.value]))
            source = "bandit:thompson"

        a, b = table[arm.value]
        return RouteSuggestion(
            strategy=arm,
            source=source,
            confidence=a / (a + b) if (a + b) > 0 else 0.0,
        )

    # ------------------------------------------------------------------ 反馈

    def record(
        self,
        task: str,
        registry: ToolRegistry,
        chosen: Strategy,
        outcome: RouteOutcome,
    ) -> None:
        """回灌一次结果：连续奖励做 fractional update。"""
        features = TaskFeatures.from_task(task, registry)
        key = self._key(features)
        table = self._state.get(key)
        if table is None:
            # 正常流程先 suggest 后 record；防御性兜底：未播种过则用均匀先验。
            table = self._seed(None)
            self._state[key] = table

        reward = max(0.0, min(1.0, outcome.reward))
        a, b = table[chosen.value]
        table[chosen.value] = [a + reward, b + (1.0 - reward)]
        self._obs[key] = self._obs.get(key, 0) + 1

    # ------------------------------------------------------------------ 内部

    def _key(self, features: TaskFeatures) -> tuple:
        """上下文键；非上下文模式（消融）下所有任务共享一个全局键。"""
        return features.key() if self.contextual else _GLOBAL_KEY

    def _seed(self, rule: Strategy | None) -> dict[str, list[float]]:
        table = {s.value: [_BASE_ALPHA, _BASE_BETA] for s in ARMS}
        if rule is not None:
            a, b = table[rule.value]
            table[rule.value] = [a + self.prior_strength, b]
        return table

    # ------------------------------------------------------------- 观测/调试

    def arm_means(self, features: TaskFeatures) -> dict[str, float]:
        """该上下文各 arm 的 Beta 均值（解析值，不采样），供 benchmark/UI。"""
        table = self._state.get(features.key())
        if table is None:
            base = _BASE_ALPHA / (_BASE_ALPHA + _BASE_BETA)
            return {s.value: base for s in ARMS}
        return {arm: a / (a + b) if (a + b) > 0 else 0.0 for arm, (a, b) in table.items()}

    def observed_count(self, features: TaskFeatures) -> int:
        """该上下文累计的真实观测条数。"""
        return self._obs.get(features.key(), 0)

    def contexts(self) -> list[tuple]:
        """已见过的上下文键列表。"""
        return list(self._state.keys())

    # --------------------------------------------------------------- 持久化

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _STATE_VERSION,
            "params": {
                "epsilon": self.epsilon,
                "min_samples": self.min_samples,
                "prior_strength": self.prior_strength,
                "contextual": self.contextual,
            },
            "obs": {
                json.dumps(list(k), ensure_ascii=False): n for k, n in self._obs.items()
            },
            "state": {
                json.dumps(list(k), ensure_ascii=False): {
                    arm: [a, b] for arm, (a, b) in table.items()
                }
                for k, table in self._state.items()
            },
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, seed: int | None = None
    ) -> ContextualBanditSelector:
        params = data.get("params", {})
        obj = cls(
            seed=seed,
            epsilon=float(params.get("epsilon", _DEFAULT_EPSILON)),
            min_samples=float(params.get("min_samples", _DEFAULT_MIN_SAMPLES)),
            prior_strength=float(params.get("prior_strength", _DEFAULT_PRIOR_STRENGTH)),
            contextual=bool(params.get("contextual", True)),
        )
        obj._state = {
            tuple(json.loads(k)): {
                arm: [float(a), float(b)] for arm, (a, b) in table.items()
            }
            for k, table in data.get("state", {}).items()
        }
        obj._obs = {
            tuple(json.loads(k)): int(n) for k, n in data.get("obs", {}).items()
        }
        return obj

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(
        cls, path: str | Path, *, seed: int | None = None
    ) -> ContextualBanditSelector:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data, seed=seed)
