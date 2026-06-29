"""
GrowthState — 桌宠养成状态

Delta 驱动设计：
- 代码是状态机裁决者
- 模型只能提议变化（通过 apply_delta）
- 程序校验 + clamp + 写库

Phase 1 只实现基础读写。Phase 3 再实现 apply_delta / record_successful_turn / derive_stage。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

DEFAULT_USER_ID = "local-default"
DEFAULT_COMPANION_ID = "default-pet"


@dataclass(slots=True)
class GrowthState:
    affection: int = 0
    mood: str = "calm"
    stage: str = "stranger"
    streak: int = 0
    conversation_count: int = 0
    last_seen: float = 0.0


class GrowthStore:
    """陪伴养成状态持久化存储。"""

    def __init__(
        self,
        store,
        user_id: str = DEFAULT_USER_ID,
        companion_id: str = DEFAULT_COMPANION_ID,
    ):
        self._store = store
        self.user_id = user_id
        self.companion_id = companion_id

    def get(self) -> GrowthState:
        """从 growth_state 表读取状态。记录不存在时插入默认值。"""
        conn = self._store._connect()
        row = conn.execute(
            "SELECT affection, mood, stage, streak, conversation_count, last_seen "
            "FROM growth_state WHERE user_id = ? AND companion_id = ?",
            (self.user_id, self.companion_id),
        ).fetchone()

        if row is None:
            state = GrowthState()
            self.save(state)
            return state

        return GrowthState(
            affection=row["affection"],
            mood=row["mood"],
            stage=row["stage"],
            streak=row["streak"],
            conversation_count=row["conversation_count"],
            last_seen=row["last_seen"],
        )

    def save(self, state: GrowthState) -> None:
        """原子 upsert。本阶段只提供安全写入能力。"""
        conn = self._store._connect()
        conn.execute(
            "INSERT OR REPLACE INTO growth_state "
            "(user_id, companion_id, affection, mood, stage, streak, "
            " conversation_count, last_seen, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.user_id,
                self.companion_id,
                state.affection,
                state.mood,
                state.stage,
                state.streak,
                state.conversation_count,
                state.last_seen,
                time.time(),
            ),
        )

    def format_for_prompt(self) -> str:
        state = self.get()
        return (
            "【陪伴状态】\n"
            f"- 好感度：{state.affection}/100\n"
            f"- 心情：{state.mood}\n"
            f"- 关系阶段：{state.stage}\n"
            f"- 连续陪伴：{state.streak} 天\n"
            f"- 累计对话：{state.conversation_count} 轮"
        )

    # ── Phase 3 TODO ──────────────────────────────────────────
    #
    # def record_successful_turn(self, now: float, timezone: str) -> GrowthState:
    #     """
    #     必须先读取旧 last_seen，再计算 streak，最后统一写入。
    #     禁止先覆盖 last_seen 再计算 streak。
    #
    #     规则：
    #     - 同一自然日再次聊天：streak 不变
    #     - 上次聊天日期为昨天：streak += 1
    #     - 间隔超过一天：streak = 1
    #     - conversation_count 每个成功用户轮次 +1
    #     - 工具内部重试、fallback、确认弹窗、ReAct 内部循环不得重复累计
    #     """
    #
    # def apply_delta(self, delta: dict) -> GrowthState:
    #     """
    #     模型只能"建议"变化，程序负责裁决。
    #
    #     规则：
    #     - affection 单轮限幅：[-2, +2]
    #     - affection 最终 clamp 到 [0, 100]
    #     - mood 必须来自枚举白名单
    #     - stage 不允许模型直接修改
    #     - stage 必须由程序派生
    #     """
    #
    # def derive_stage(self, state: GrowthState) -> str:
    #     """
    #     第一版建议：
    #     - affection >= 70 且 conversation_count >= 80 → close
    #     - affection >= 25 且 conversation_count >= 15 → familiar
    #     - 其他 → stranger
    #     """
