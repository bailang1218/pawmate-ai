import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pawmate.desktop_pet.runtime.motion_graph import MotionRuntimeSimulator, MotionState, IntentRouter, PetIntentType

fails=[]
def ck(name, cond):
    print(("PASS " if cond else "FAIL ")+name);
    if not cond: fails.append(name)

def play_from_work(ev):
    sim = MotionRuntimeSimulator(initial_state=MotionState.STANDING_IDLE)
    sim.receive("task_start", turn_id=1); sim.run_until_settled()
    assert sim.current_state==MotionState.WORKING_IDLE, sim.current_state
    sim.receive(ev, turn_id=2); played = sim.run_until_settled()
    return sim.current_state, played

for ev in ["chat_normal","user_input_received","short_task","memory_task","user_praise","sad","user_upload_received"]:
    st, played = play_from_work(ev)
    stood = any(c in ("15_put_away","19_stand_up") for c in played)
    ck(f"work + {ev}: 不站起来 (state={st.value}, played={played})", (not stood) and st==MotionState.WORKING_IDLE)

# 睡眠态不被情绪表演拽起
sim = MotionRuntimeSimulator(initial_state=MotionState.SLEEPING_IDLE)
sim.receive("chat_normal", turn_id=9); played = sim.run_until_settled()
ck(f"sleep + chat_normal: 不醒不站 (state={sim.current_state.value}, played={played})",
   sim.current_state==MotionState.SLEEPING_IDLE and not played)

# 回归：站立态正常表演仍然工作
sim = MotionRuntimeSimulator(initial_state=MotionState.STANDING_IDLE)
sim.receive("chat_normal", turn_id=1); played = sim.run_until_settled()
ck(f"stand + chat_normal: 仍播表演 (played={played})", "09_talking_norm" in played)

sim = MotionRuntimeSimulator(initial_state=MotionState.STANDING_IDLE)
sim.receive("user_praise", turn_id=1); played = sim.run_until_settled()
ck(f"stand + user_praise: 仍播开心 (played={played})", "12_happy_jumping" in played)

# 回归：工作流程正常的 in-place 反馈仍在 (busy->14_frustrated)
sim = MotionRuntimeSimulator(initial_state=MotionState.STANDING_IDLE)
sim.receive("task_start", turn_id=1); sim.run_until_settled()
sim.receive("busy", turn_id=1); played = sim.run_until_settled()
ck(f"work + busy: 原地 frustrated (state={sim.current_state.value}, played={played})",
   sim.current_state==MotionState.WORKING_IDLE and "14_frustrated" in played)

# 回归：work_complete 仍能正常收尾回到 sitting
sim = MotionRuntimeSimulator(initial_state=MotionState.STANDING_IDLE)
sim.receive("task_start", turn_id=1); sim.run_until_settled()
sim.receive("work_complete", turn_id=1); played = sim.run_until_settled()
ck(f"work + work_complete: 收尾回 sitting (state={sim.current_state.value}, played={played})",
   sim.current_state==MotionState.SITTING_IDLE and "15_put_away" in played)

# 图仍强连通（validate 不抛）
from pawmate.desktop_pet.runtime.motion_graph import default_motion_graph
try:
    default_motion_graph(); ck("graph 强连通校验通过", True)
except Exception as e:
    ck(f"graph 校验: {e}", False)

print("\n=== ", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
if __name__ == "__main__":
    sys.exit(1 if fails else 0)
