"""Locked, versioned scenarios used by both the dashboard and CLI benchmark."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass

MODE_UNCACHED = "uncached-extreme"
MODE_DAILY = "daily-dialogue"
MODE_CODEX = "codex-high-cache"
DEFAULT_MODE_KEY = MODE_DAILY


_BUGGY_CODE = '''
import time
from collections import defaultdict


class SessionAggregator:
    """Merge user events into sessions and expose activity statistics."""

    def __init__(self, max_idle=1800, cache=defaultdict(list)):
        self.max_idle = max_idle
        self.cache = cache
        self.result = {}

    def merge(self, sessions, now_ts):
        for source in sessions:
            uid = source["user_id"]
            if uid not in self.result:
                self.result[uid] = {"events": [], "last": 0, "count": 0}
            for event in source["events"]:
                if event["ts"] - self.result[uid]["last"] > self.max_idle:
                    self.result[uid]["count"] += 1
                self.result[uid]["events"].append(event)
                self.result[uid]["last"] = event["ts"]
                self.cache[uid].append(event["ts"])
        for uid in self.result:
            idle = now_ts - self.result[uid]["last"]
            self.result[uid]["active"] = idle <= self.max_idle
            self.result[uid]["sessions"] = self.result[uid]["count"]
        return self.result

    def top_active(self, n=5):
        items = sorted(
            self.result.items(),
            key=lambda item: item[1]["last"],
            reverse=True,
        )
        return [uid for uid, _ in items if self.result[uid]["active"]][:n]

    def idle_ratio(self):
        total = len(self.result)
        idle = sum(1 for value in self.result.values() if not value["active"])
        return idle / total

    def session_spans(self, uid):
        events = self.result[uid]["events"]
        spans, start, previous = [], None, None
        for event in events:
            if start is None:
                start = event["ts"]
            elif event["ts"] - previous > self.max_idle:
                spans.append((start, previous))
                start = event["ts"]
            previous = event["ts"]
        spans.append((start, previous))
        return spans

    def merge_incremental(self, sessions, now_ts):
        for source in sessions:
            uid = source["user_id"]
            seen = set(self.cache[uid])
            fresh = [event for event in source["events"] if event["ts"] not in seen]
            self.merge([{"user_id": uid, "events": fresh}], now_ts)
        return self.result

    def flush(self, older_than_ts):
        for uid in list(self.result):
            if self.result[uid]["last"] < older_than_ts:
                del self.result[uid]


def summarize(sessions, now_ts=None):
    now_ts = now_ts or time.time()
    aggregator = SessionAggregator()
    merged = aggregator.merge(sessions, now_ts)
    return {
        "users": len(merged),
        "active": [uid for uid in merged if merged[uid]["active"]],
        "idle_ratio": aggregator.idle_ratio(),
        "hot": aggregator.top_active(),
        "spans": {uid: aggregator.session_spans(uid) for uid in merged},
    }
'''.strip()


_CODE_REVIEW_SYSTEM = (
    "你是资深 Python 工程师。请对下面的线上会话聚合模块做可执行的代码评审与重构。"
    "每次请求都是独立任务，不引用之前的回答。\n\n"
    "业务契约与数据约束（修复时必须保持）：\n"
    "- sessions 是来自多个分片的可迭代对象；同一 user_id 可出现在多个分片，分片之间不保证顺序。\n"
    "- 每个合法事件至少包含整数或浮点数 ts；时间戳相同的事件按原始出现次序稳定处理。\n"
    "- 两个相邻事件的差值严格大于 max_idle 才开启新会话，等于 max_idle 仍属于同一段。\n"
    "- active 只由 now_ts 与该用户最后一个合法事件决定；未来时间戳按 active 处理但要说明风险。\n"
    "- result 必须可重复调用 merge；重复输入不能让事件、会话数或 cache 无限膨胀。\n"
    "- 缺少 user_id 的来源、缺少 ts 或 ts 非有限数字的事件不能悄悄污染结果；选择跳过或抛错时要统一。\n"
    "- top_active 先按最后活动时间降序，再以 user_id 的稳定字符串表示打破平局，保证跨进程可复现。\n"
    "- flush 必须同步清理 result 与对应缓存；清理后再次收到该用户事件应像新用户一样工作。\n"
    "- summarize 的键名与值类型保持兼容：users 为整数、active/hot 为列表、idle_ratio 为浮点数、spans 为映射。\n"
    "- 不引入第三方依赖；实现以 Python 3.10+ 为目标，并避免依赖字典碰巧保持的业务顺序。\n\n"
    "待处理模块：\n```python\n"
    f"{_BUGGY_CODE}\n"
    "```\n\n"
    "必须覆盖以下约束：\n"
    "1. 找出事件未按 ts 排序、首事件 last=0、可变默认参数、跨来源全局有序假设、"
    "空集合除零、缺字段、重复事件和 flush 后缓存残留等问题；逐条说明触发条件与后果。\n"
    "2. 分析时间和空间复杂度，并解释乱序输入为何让 count、active、top_active 不确定。\n"
    "3. 给出完整修复实现：稳定排序、正确分段、None 哨兵、防御性校验、去重策略、"
    "空输入返回 0.0，并保持 summarize 的返回结构。\n"
    "4. 写至少六个边界测试，覆盖空输入、乱序、单事件、多用户跨空闲段、重复事件、缺字段。\n"
    "5. 说明增量合并与全量合并如何保持同一语义，并给出失败时的错误策略。\n\n"
    "输出固定为：\n## 缺陷清单\n## 复杂度与不确定性\n## 修复实现\n## 边界测试\n"
    "只输出这四节，不要寒暄和额外总结。"
)

_CODE_REVIEW_USER = (
    "从头完整处理上面的 SessionAggregator 任务。严格按四个固定小节输出，给出可运行实现和具体测试，不要省略代码。"
)

_DAILY_SYSTEM = (
    "你是日常结对编程助手。回答要直接、务实，优先给最小可维护改动；"
    "保留上下文中的既有约束，不虚构运行结果。代码与解释合计控制在适中长度。"
)

_DAILY_TURNS = (
    """我有个 Python 函数偶尔漏数据，请先定位两个最可能的根因并给最小修复：
```python
def latest_by_user(rows):
    result = {}
    for row in rows:
        uid = row.get("user_id")
        if not uid:
            continue
        if uid not in result or row["ts"] > result[uid]["ts"]:
            result[uid] = row
    return result
```
输入可能有 `ts=None`、重复记录，也可能被调用方继续修改。""",
    "把刚才的修复整理成完整函数：不要修改输入对象，重复记录要确定性处理，并补上类型标注。",
    "为这个函数补 4 个 pytest 风格测试，只写关键断言；覆盖空输入、None 时间、重复和调用方后续修改。",
    "现在从代码审查角度检查上一版：哪些行为还没有明确契约？请给推荐默认值，不要大改接口。",
    "把实现再精简一遍，优先可读性；解释为什么排序或单次遍历二选一更适合这里。",
    "最后给一份可以直接贴进 PR 描述的变更摘要、风险和验证清单，不要声称已经运行测试。",
)


@dataclass(frozen=True)
class TestMode:
    key: str
    name: str
    badge: str
    description: str
    methodology: str
    cache_policy: str
    default_reasoning: str
    allowed_reasoning: tuple[str, ...]
    system_prompt: str
    turn_prompts: tuple[str, ...]
    stateful: bool
    default_max_requests: int
    request_interval_seconds: float
    max_output_tokens: int
    warmup_rounds: int
    cache_ratio_min: float = 0.0
    cache_ratio_max: float = 1.0
    version: int = 3

    def public_dict(self) -> dict[str, object]:
        data = asdict(self)
        data.pop("system_prompt", None)
        data.pop("turn_prompts", None)
        return data


TEST_MODES: dict[str, TestMode] = {
    MODE_UNCACHED: TestMode(
        key=MODE_UNCACHED,
        name="无缓存极限额度",
        badge="理论边界",
        description="每次把等长随机标记放在提示词最前端，主动击穿前缀缓存；使用长编码任务与高推理，测最苛刻的 API 等效消耗。",
        methodology="不代表真实日常使用；正式结果要求缓存读取率不高于 1%。",
        cache_policy="none",
        default_reasoning="xhigh",
        allowed_reasoning=("high", "xhigh"),
        system_prompt=_CODE_REVIEW_SYSTEM,
        turn_prompts=(_CODE_REVIEW_USER,),
        stateful=False,
        default_max_requests=6,
        request_interval_seconds=0.75,
        max_output_tokens=6000,
        warmup_rounds=1,
        cache_ratio_max=0.01,
    ),
    MODE_DAILY: TestMode(
        key=MODE_DAILY,
        name="日常对话额度",
        badge="推荐基线",
        description="六轮真实结对编程对话，保留并增长上下文；短问答、修复、测试和复查混合，缓存率随上下文自然变化。",
        methodology="固定 medium 推理；每六轮开启一段新对话，避免无限增长，也不人为追求缓存命中。",
        cache_policy="natural",
        default_reasoning="medium",
        allowed_reasoning=("medium",),
        system_prompt=_DAILY_SYSTEM,
        turn_prompts=_DAILY_TURNS,
        stateful=True,
        default_max_requests=12,
        request_interval_seconds=1.5,
        max_output_tokens=1600,
        warmup_rounds=1,
    ),
    MODE_CODEX: TestMode(
        key=MODE_CODEX,
        name="Codex 高缓存额度",
        badge="high / xhigh",
        description="固定长代码上下文与固定请求逐字节复用，并使用稳定 prompt_cache_key；模拟 Codex high/xhigh 且缓存命中很高的连续任务。",
        methodology="首条用于预热；正式结果要求缓存读取率至少 75%，否则标记为口径未达标。",
        cache_policy="high",
        default_reasoning="high",
        allowed_reasoning=("high", "xhigh"),
        system_prompt=_CODE_REVIEW_SYSTEM,
        turn_prompts=(_CODE_REVIEW_USER,),
        stateful=False,
        default_max_requests=8,
        request_interval_seconds=0.75,
        max_output_tokens=6000,
        warmup_rounds=1,
        cache_ratio_min=0.75,
    ),
}


def get_test_mode(key: str) -> TestMode:
    normalized = str(key or DEFAULT_MODE_KEY).strip()
    try:
        return TEST_MODES[normalized]
    except KeyError as error:
        raise ValueError(f"未知测试模式: {normalized}") from error


def public_test_modes() -> dict[str, dict[str, object]]:
    return {key: mode.public_dict() for key, mode in TEST_MODES.items()}


def daily_turn_prompt(round_number: int) -> str:
    index = (max(int(round_number), 1) - 1) % len(_DAILY_TURNS)
    return _DAILY_TURNS[index]


def is_daily_cycle_start(round_number: int) -> bool:
    return (max(int(round_number), 1) - 1) % len(_DAILY_TURNS) == 0


def cache_buster(seed: str) -> str:
    """Return 128 stable-token-count A/B markers derived from ``seed``."""
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    bits = "".join(f"{byte:08b}" for byte in digest)
    symbols: Sequence[str] = tuple("A" if bit == "0" else "B" for bit in bits[:128])
    return "CACHE_BUSTER " + " ".join(symbols)


def prompt_cache_key(mode_key: str, model: str, *, run_id: str, round_number: int) -> str:
    mode = get_test_mode(mode_key)
    if mode_key == MODE_UNCACHED:
        source = f"{mode.key}:v{mode.version}:{model}:{run_id}:{round_number}"
    else:
        source = f"token-benchmark-v{mode.version}:{mode.key}:{model}"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
    return f"tb-{digest}"
