"""
Scheduler tools — registered as ToolDef for the engine's ToolRegistry.

4 tools: schedule_once / schedule_daily / list_scheduled_tasks / cancel_task
"""
import logging
from pawmate.tools.core.registry import ToolDef, APPROVAL_AUTO, APPROVAL_CONFIRM
from pawmate.scheduling.scheduler import PawScheduler

logger = logging.getLogger("pawmate.tools.scheduler")


def register_scheduler_tools(registry, scheduler: PawScheduler):

    async def _schedule_once(delay_seconds: int, action: str) -> str:
        if delay_seconds < 1:
            return "✗ delay_seconds must be >= 1"
        if delay_seconds > 30 * 24 * 3600:
            return "✗ delay_seconds too long (max 30 days)"
        if not action.strip():
            return "✗ action cannot be empty"
        task_id = scheduler.schedule_once(delay_seconds, action)
        logger.info("[Scheduler] once task=%s delay=%ds action=%.60s", task_id, delay_seconds, action)
        return f"✓ Scheduled one-time task [{task_id}], will fire in {delay_seconds}s: {action}"

    async def _schedule_daily(hour: int, minute: int, action: str) -> str:
        if not (0 <= hour <= 23):
            return "✗ hour must be 0-23"
        if not (0 <= minute <= 59):
            return "✗ minute must be 0-59"
        if not action.strip():
            return "✗ action cannot be empty"
        task_id = scheduler.schedule_daily(hour, minute, action)
        logger.info("[Scheduler] daily task=%s %02d:%02d action=%.60s", task_id, hour, minute, action)
        return f"✓ Scheduled daily task [{task_id}] at {hour:02d}:{minute:02d}: {action}"

    async def _list_scheduled_tasks() -> str:
        tasks = scheduler.list_tasks()
        if not tasks:
            return "(No scheduled tasks)"
        lines = [f"Total {len(tasks)} scheduled tasks:"]
        for t in tasks:
            lines.append(f"- [{t['id']}] {t['name']}")
            lines.append(f"  Next fire: {t['next_run'] or 'stopped'}")
        return "\n".join(lines)

    async def _cancel_task(task_id: str) -> str:
        if scheduler.cancel(task_id):
            return f"✓ Cancelled task {task_id}"
        return f"✗ Task {task_id} not found (use list_scheduled_tasks to see active tasks)"

    tools = [
        ToolDef(
            name="schedule_once",
            description=(
                "安排一次性定时任务：从现在算起 delay_seconds 秒后，"
                "自动执行 action 描述的动作。"
                "适用：主人说'N 分钟/小时/天后做某事'。"
                "不适用：10 秒内可立刻完成的动作——直接做，不要安排。"
                "示例：主人说'2 分钟后开百度' → delay_seconds=120, "
                "action='打开百度首页，并告诉主人已打开'"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "delay_seconds": {
                        "type": "integer",
                        "description": "多少秒后触发。1 分钟=60，1 小时=3600，1 天=86400。",
                        "minimum": 1,
                        "maximum": 2592000,
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "到时候要做什么，用第二人称自然语言写给'未来的自己'，"
                            "描述清楚要调什么工具、要向主人汇报什么。"
                            "示例：'打开百度搜索天气，把结果总结成一句话告诉主人'"
                        ),
                    },
                },
                "required": ["delay_seconds", "action"],
            },
            handler=_schedule_once,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="schedule_daily",
            description=(
                "安排每日定时任务：每天 hour:minute 时刻自动执行 action。"
                "适用：主人说'每天某时做某事'、'每天提醒我...'。"
                "示例：主人说'每天早上 9 点提醒我喝水' → "
                "hour=9, minute=0, action='提醒主人喝水'"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hour": {
                        "type": "integer",
                        "description": "小时，24 小时制",
                        "minimum": 0,
                        "maximum": 23,
                    },
                    "minute": {
                        "type": "integer",
                        "description": "分钟",
                        "minimum": 0,
                        "maximum": 59,
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "到点要做什么，第二人称自然语言。"
                            "示例：'打开微信，提醒主人开晨会'"
                        ),
                    },
                },
                "required": ["hour", "minute", "action"],
            },
            handler=_schedule_daily,
            approval=APPROVAL_CONFIRM,
        ),
        ToolDef(
            name="list_scheduled_tasks",
            description=(
                "列出所有已安排的定时任务，"
                "返回每个任务的 task_id、名称、下次触发时间。"
                "什么时候用："
                "(1) 主人问'我安排了什么任务'；"
                "(2) 准备 cancel_task 前确认 task_id；"
                "(3) 准备安排新任务前检查是否已有重复任务。"
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_list_scheduled_tasks,
            approval=APPROVAL_AUTO,
        ),
        ToolDef(
            name="cancel_task",
            description=(
                "按 task_id 取消一个已安排的定时任务。"
                "如果不知道 task_id，先调 list_scheduled_tasks 查。"
                "适用：主人说'取消那个任务'、'不用提醒我了'、'忘掉刚才的安排'等。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": (
                            "任务 id，格式形如 'once_xxxxxxxx' 或 'daily_xxxxxxxx'，"
                            "从 list_scheduled_tasks 的返回中获取，不要自己编造。"
                        ),
                    },
                },
                "required": ["task_id"],
            },
            handler=_cancel_task,
            approval=APPROVAL_CONFIRM,
        ),
    ]

    for t in tools:
        registry.register(t)
    logger.info("[Scheduler] 4 scheduler tools registered")
