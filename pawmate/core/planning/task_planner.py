"""
任务规划器模块

提供高级任务规划和分解能力
使AI能够制定和执行复杂的多步骤计划
"""
import json
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
import re
from enum import Enum


logger = logging.getLogger("pawmate")


class TaskComplexity(Enum):
    """任务复杂度等级"""
    SIMPLE = "simple"           # 简单：一个命令或工具调用就能完成
    MODERATE = "moderate"       # 中等：需要2-3个步骤
    COMPLEX = "complex"         # 复杂：需要4步以上或多个子目标
    UNKNOWN = "unknown"         # 未知


@dataclass
class Task:
    """表示单个任务"""
    id: str
    description: str
    status: str  # 'pending', 'running', 'completed', 'failed'
    dependencies: List[str]  # 依赖的任务ID
    priority: int = 1  # 1-10, 10为最高优先级
    estimated_duration: Optional[int] = None  # 预估持续时间（秒）
    created_at: datetime = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[str] = None
    error: Optional[str] = None
    complexity: TaskComplexity = TaskComplexity.UNKNOWN  # 任务复杂度
    subtasks: List['Task'] = field(default_factory=list)  # 如果被分解，存储子任务
    parent_id: Optional[str] = None  # 父任务ID

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


@dataclass
class Plan:
    """任务计划"""
    id: str
    description: str
    tasks: List[Task]
    status: str  # 'pending', 'running', 'completed', 'failed', 'cancelled'
    created_at: datetime = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    current_task_index: int = 0

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


class TaskPlanner:
    """
    任务规划器 - 用于高级AI规划和执行复杂任务
    支持 LLM 驱动的智能分解和复杂度评估
    """
    def __init__(self, llm_client=None):
        self.plans: Dict[str, Plan] = {}
        self.current_plan: Optional[Plan] = None
        self.task_results: Dict[str, Any] = {}
        self.llm_client = llm_client  # 可选的LLM客户端，用于智能分解

    _MULTI_STEP_HINTS = re.compile(
        r"(然后|接下来|之后|最后|同时|先.*再|创建.*并.*写|打开.*并.*写|生成.*并.*运行|写.*并.*运行|"
        r"\bthen\b|\bfirst\b|\bfinally\b|\bafter\b|\band then\b|check.*run.*report)",
        re.IGNORECASE,
    )
    _SINGLE_ACTION_HINTS = re.compile(
        r"(填(写|入|进)?|输入|点击|点一下|选择|打开|访问|进入|截图|截屏|查询|查一下|搜索|复制|粘贴|提交|刷新)"
    )

    def _needs_planning_hint(self, user_input: str) -> bool:
        """用很便宜的启发式先判断是否明显需要规划。"""
        text = (user_input or "").strip()
        if not text:
            return False

        # 单个具体工具动作交给 ReAct 自己完成；误规划比漏规划更容易把任务带偏。
        if self._SINGLE_ACTION_HINTS.search(text) and not self._MULTI_STEP_HINTS.search(text):
            return False

        if self._MULTI_STEP_HINTS.search(text):
            return True

        action_words = [
            "创建", "新建", "写", "运行", "执行", "打开", "下载", "安装", "配置",
            "生成", "修改", "整理", "导出", "部署", "测试", "调试", "写入", "保存",
            "搜索", "查询", "总结", "分析",
        ]
        action_count = sum(1 for word in action_words if word in text)
        return action_count >= 3

    async def plan_with_llm(self, user_input: str) -> Optional[Plan]:
        """
        用 LLM 一次性判断是否需要规划 + 生成任务列表 + 标注依赖。

        返回:
            Plan 对象（需要规划时）或 None（简单请求，直接走 ReAct）
        """
        if not self.llm_client:
            return None

        if not user_input or not user_input.strip():
            return None

        # 明显的单步请求直接走 ReAct，省去一次 LLM 规划往返。
        if not self._needs_planning_hint(user_input):
            return None

        system_prompt = (
            "你是任务规划助手。判断用户请求是否需要拆解成多步骤执行，以支持良好的UI反馈。\n\n"
            "输出严格的 JSON，不要 markdown 代码块，不要任何解释文字：\n\n"
            "{\n"
            "  \"needs_planning\": true/false,\n"
            "  \"reason\": \"简短说明\",\n"
            "  \"tasks\": [\n"
            "    {\n"
            "      \"id\": \"t1\",\n"
            "      \"description\": \"具体可执行的步骤描述\",\n"
            "      \"depends_on\": [],\n"
            "      \"expected_output\": \"期望的该步输出，用于UI显示\"\n"
            "    }\n"
            "  ]\n"
            "}\n\n"
            "判断标准：\n"
            "- 单一具体动作（打开应用/网页、点击、填表单、截图、单次查询、回答简单问题）→ needs_planning=false, tasks=[]\n"
            "- 多动作串联（如：搜索→分析→下载→保存）→ needs_planning=true\n"
            "- 需要验证中间结果（分析→验证→修正→完成）→ needs_planning=true\n"
            "- 闲聊、概念解释 → needs_planning=false\n\n"
            "要求：\n"
            "- 步骤数不超过 6 个\n"
            "- tasks 只能来自用户请求中实际表达的子目标，禁止补出用户没有要求的步骤\n"
            "- 不要自行加入等待用户输入、等待验证码、组合验证码、登录验证等假设性流程\n"
            "- 拿不准是否需要拆解时，返回 needs_planning=false, tasks=[]\n"
            "- 每步必须是具体可执行的动作，不要空泛描述\n"
            "- depends_on 只列必须前置完成的步骤 id；可并行的步骤不要硬串成链\n"
            "- 步骤 id 用 t1/t2/t3 格式\n"
            "- expected_output 应简洁明了，适合在UI进度条中显示\n"
            "- 每步完成后，系统会显示该步的执行结果，便于用户追踪"
        )

        try:
            full_text = ""
            async for event in self.llm_client.stream(
                messages=[{"role": "user", "content": user_input}],
                system=system_prompt,
                tools=None,
            ):
                if event.get("type") == "text_delta":
                    full_text += event.get("text", "")

            cleaned = full_text.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()

            data = json.loads(cleaned)

            if not data.get("needs_planning") or not data.get("tasks"):
                return None

            tasks: List[Task] = []
            for t in data.get("tasks", []):
                if not t.get("id") or not t.get("description"):
                    continue
                tasks.append(Task(
                    id=str(t["id"]),
                    description=str(t["description"]),
                    status="pending",
                    dependencies=list(t.get("depends_on", [])),
                    priority=5,
                    complexity=TaskComplexity.MODERATE,
                    # 使用预期输出作为任务的简洁描述，便于UI显示
                    result=str(t.get("expected_output", t["description"]))[:50]  # 简化版用于UI显示
                ))

            if not tasks:
                return None

            plan_id = f"plan_{int(datetime.now().timestamp())}"
            plan = Plan(
                id=plan_id,
                description=user_input,
                tasks=tasks,
                status="pending",
            )
            self.plans[plan_id] = plan
            self.current_plan = plan
            return plan

        except (json.JSONDecodeError, KeyError, Exception) as e:
            logger.warning("[TaskPlanner] LLM planning failed; falling back to no-plan mode: %s", e)
            return None

    def _check_dependencies_satisfied(self, task: Task, plan: Plan) -> bool:
        """检查任务的所有依赖是否已在当前计划内完成。"""
        task_map = {t.id: t for t in plan.tasks}
        for dep_id in task.dependencies:
            dep_task = task_map.get(dep_id)
            if dep_task is None:
                logger.warning("[TaskPlanner] dependency %s was not found in plan", dep_id)
                return False
            if dep_task.status != "completed":
                return False
        return True

    def get_plan_status(self, plan_id: str) -> Dict[str, Any]:
        """
        获取计划的详细状态

        Args:
            plan_id: 计划ID

        Returns:
            计划状态信息
        """
        if plan_id not in self.plans:
            return {"error": f"Plan {plan_id} not found"}

        plan = self.plans[plan_id]
        completed_tasks = sum(1 for task in plan.tasks if task.status == 'completed')
        total_tasks = len(plan.tasks)

        return {
            "plan_id": plan.id,
            "description": plan.description,
            "status": plan.status,
            "progress": {
                "completed": completed_tasks,
                "total": total_tasks,
                "percentage": (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
            },
            "created_at": plan.created_at.isoformat() if plan.created_at else None,
            "started_at": plan.started_at.isoformat() if plan.started_at else None,
            "completed_at": plan.completed_at.isoformat() if plan.completed_at else None,
            "tasks": [
                {
                    "id": task.id,
                    "description": task.description,
                    "status": task.status,
                    "priority": task.priority,
                    "estimated_duration": task.estimated_duration,
                    "result_summary": task.result[:100] + "..." if task.result and len(task.result) > 100 else task.result
                }
                for task in plan.tasks
            ]
        }

    def cancel_plan(self, plan_id: str) -> bool:
        """
        取消计划执行

        Args:
            plan_id: 计划ID

        Returns:
            True 如果成功取消
        """
        if plan_id not in self.plans:
            return False

        plan = self.plans[plan_id]
        if plan.status in ['completed', 'failed']:
            return False  # 已完成的计划无法取消

        plan.status = 'cancelled'
        return True

    def get_active_plans(self) -> List[str]:
        """获取所有活跃计划的ID"""
        return [
            plan_id for plan_id, plan in self.plans.items()
            if plan.status in ['pending', 'running']
        ]

    def reset(self):
        """重置规划器状态"""
        self.plans.clear()
        self.current_plan = None
        self.task_results.clear()
