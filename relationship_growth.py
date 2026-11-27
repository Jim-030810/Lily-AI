"""
relationship_growth.py
Tracks relationship progression between Lily and the user: meaningful
conversation turns, milestone moments, and stage-appropriate prompt
guidance.
"""

import time

STAGE_THRESHOLDS = [
    (0, "stranger"),
    (5, "acquaintance"),
    (15, "friend"),
    (30, "close_friend"),
    (50, "confidant"),
]

STAGE_GUIDANCE_EN = {
    "stranger": "You are just getting to know this person. Be polite, a little reserved, and curious about them.",
    "acquaintance": "You are becoming familiar with this person. Be warmer and more casual than with a stranger.",
    "friend": "You consider this person a friend. Be relaxed, playful, and open with them.",
    "close_friend": "You are close with this person. Be affectionate, comfortable, and show you remember shared history.",
    "confidant": "You deeply trust this person. Be warm, vulnerable when appropriate, and speak as someone who truly knows them.",
}

STAGE_GUIDANCE_JA = {
    "stranger": "まだ知り合ったばかりです。礼儀正しく、少し控えめに、相手に興味を持って接してください。",
    "acquaintance": "だんだん親しくなってきています。他人行儀すぎず、少しカジュアルに話してください。",
    "friend": "相手を友達だと思っています。リラックスして、気さくに話してください。",
    "close_friend": "相手と親しい関係です。親しみを込めて、これまでの思い出を大切にしながら話してください。",
    "confidant": "相手を深く信頼しています。温かく、時には素直な気持ちも見せながら話してください。",
}


def _get_relationship(mem):
    if "relationship" not in mem or not isinstance(mem.get("relationship"), dict):
        mem["relationship"] = {
            "meaningful_turns": 0,
            "milestones": [],
            "last_turn_ts": None,
        }
    rel = mem["relationship"]
    rel.setdefault("meaningful_turns", 0)
    rel.setdefault("milestones", [])
    rel.setdefault("last_turn_ts", None)
    return rel


def _get_stage(mem):
    rel = _get_relationship(mem)
    turns = rel.get("meaningful_turns", 0)
    level = mem.get("level", 0) if isinstance(mem, dict) else 0
    score = turns + level
    stage = "stranger"
    for threshold, name in STAGE_THRESHOLDS:
        if score >= threshold:
            stage = name
    return stage


def record_meaningful_turn(mem, user_input):
    """Call after a real exchange with the user to log relationship progress."""
    if not user_input or not str(user_input).strip():
        return
    rel = _get_relationship(mem)
    rel["meaningful_turns"] += 1
    rel["last_turn_ts"] = int(time.time())


def maybe_flag_milestone(mem, user_input, response_text, emotion, language):
    """Flag a milestone if this turn seems emotionally significant."""
    rel = _get_relationship(mem)
    turns = rel.get("meaningful_turns", 0)

    is_round_number = turns in (5, 15, 30, 50, 100, 200)
    strong_emotion = emotion in ("happy", "sad", "excited", "grateful", "vulnerable") if emotion else False

    if is_round_number or strong_emotion:
        milestone = {
            "turn_count": turns,
            "timestamp": int(time.time()),
            "trigger": "round_number" if is_round_number else f"emotion:{emotion}",
            "snippet": str(user_input)[:120],
        }
        rel["milestones"].append(milestone)
        if len(rel["milestones"]) > 50:
            rel["milestones"] = rel["milestones"][-50:]
        return milestone
    return None


def format_relationship_context(mem, language="en"):
    """Return a system-prompt string summarizing the relationship so far."""
    rel = _get_relationship(mem)
    turns = rel.get("meaningful_turns", 0)
    milestone_count = len(rel.get("milestones", []))
    stage = _get_stage(mem)

    if turns == 0:
        return ""

    if language == "ja":
        return (
            f"[関係性メモ] これまでに{turns}回の意味のある会話を交わしました。"
            f"節目となる瞬間は{milestone_count}回あります。現在の関係段階: {stage}。"
        )
    return (
        f"[Relationship note] You and this person have shared {turns} meaningful "
        f"exchanges, including {milestone_count} milestone moments. "
        f"Current relationship stage: {stage}."
    )


def get_stage_guidance(mem, language="en"):
    """Return stage-appropriate tone guidance as a system-prompt string."""
    stage = _get_stage(mem)
    table = STAGE_GUIDANCE_JA if language == "ja" else STAGE_GUIDANCE_EN
    guidance = table.get(stage)
    if not guidance:
        return ""
    return f"[Relationship stage: {stage}] {guidance}"