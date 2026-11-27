"""
evolving_traits.py

Sibling module to profile_learning.py. Where profile_learning stores
durable, dated FACTS about the user ("user is job-hunting, 2 weeks
ago"), this module stores Lily's own slowly-drifting internal state
toward that user: affinity, opinions she's formed, running jokes, and
threads she might naturally circle back to. This is the "learning"
mechanism referenced earlier -- not weight updates, but an
accumulating, self-authored, incrementally-updated relationship state.

Design goals (mirrors profile_learning.py on purpose):
  - Runs as a background task (fire-and-forget) -- never adds latency
    to the reply the user actually hears.
  - Uses a plain, non-persona LLM call for the reflection pass.
  - Changes are SMALL and INCREMENTAL by construction (capped delta,
    most passes should propose nothing). Personality doesn't lurch.
  - Lives inside the same `mem` dict as everything else
    (mem['lily_traits']), persisted via the same save_memory(mem) the
    rest of the app already uses -- no separate storage system.
  - Only reflects periodically (every N turns), not every message, to
    keep it cheap and keep behavior stable turn-to-turn.

Wire-up:
  1. In Lily_generate_response(), inject format_traits_for_prompt(mem, language)
     as an extra system message, same spot as format_profile_for_prompt.
  2. In process_response()'s NORMAL CHAT branch, alongside the existing
         asyncio.create_task(update_user_profile(mem, user_input, cleaned, CURRENT_LANGUAGE))
     add:
         asyncio.create_task(maybe_reflect_traits(mem, user_input, cleaned, CURRENT_LANGUAGE))
     Fire-and-forget -- same reasoning as profile_learning: don't await
     inline, it would add a second LLM call's worth of latency to every
     reply for no user-facing benefit.
"""

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional

import requests  # already a dependency in the main module

# Same pattern as profile_learning.py -- wire up however fits your
# project layout (import from lily_bot, or hardcode here).
OLLAMA_URL = "http://127.0.0.1:11434"
LLAMA_MODEL = "qwen2.5:7b"

REFLECTION_EVERY_N_TURNS = 12
MAX_OPINIONS = 25
MAX_RUNNING_JOKES = 10
MAX_UNRESOLVED_THREADS = 10
AFFINITY_MIN, AFFINITY_MAX = 0, 100
AFFINITY_DEFAULT = 50
MAX_AFFINITY_STEP_PER_REFLECTION = 6  # hard cap: no huge swings from one pass
MAX_TRAITS_PROMPT_CHARS = 700


def _default_traits() -> Dict[str, Any]:
    return {
        "affinity": AFFINITY_DEFAULT,
        "formed_opinions": [],       # [{"topic","opinion","confidence","formed","last_touched"}]
        "running_jokes": [],         # [str]
        "unresolved_threads": [],    # [str]
        "turns_since_reflection": 0,
    }


def _get_traits(mem: dict) -> Dict[str, Any]:
    traits = mem.setdefault("lily_traits", _default_traits())
    for k, v in _default_traits().items():
        traits.setdefault(k, v)
    return traits


# ---------------------------------------------------------------------------
# Rendering into the prompt (same shape as format_profile_for_prompt)
# ---------------------------------------------------------------------------

def format_traits_for_prompt(mem: dict, language: str = "ja") -> str:
    """Returns a compact block for injection as a system message, or ''
    if nothing has accumulated yet. Caller decides whether to inject
    (skip if empty), same as format_profile_for_prompt."""
    if not mem or not isinstance(mem, dict):
        return ""
    traits = mem.get("lily_traits")
    if not traits:
        return ""

    opinions = traits.get("formed_opinions", [])
    jokes = traits.get("running_jokes", [])
    threads = traits.get("unresolved_threads", [])
    affinity = traits.get("affinity", AFFINITY_DEFAULT)

    if not opinions and not jokes and affinity == AFFINITY_DEFAULT:
        return ""  # nothing accumulated yet, don't pad the prompt

    lines = []
    if language == "en":
        lines.append(f"[HOW THINGS HAVE DEVELOPED WITH THIS USER -- affinity: {affinity}/100]")
        lines.append("(Use naturally, as things she's actually come to think/feel over time. "
                      "Never read this back as a list, never say 'according to my notes'.)")
        if opinions:
            lines.append("Opinions she's formed:")
            for o in opinions[-MAX_OPINIONS:]:
                lines.append(f"- {o.get('topic', '')}: {o.get('opinion', '')}")
        if jokes:
            lines.append("Running jokes/callbacks: " + ", ".join(jokes[-MAX_RUNNING_JOKES:]))
        if threads:
            lines.append("Might bring up if it fits naturally: " + ", ".join(threads[-MAX_UNRESOLVED_THREADS:]))
    else:
        lines.append(f"[このユーザーとの積み重ね — affinity: {affinity}/100]")
        lines.append("（自然に、思い出したように使うこと。箇条書きをそのまま読み上げない）")
        if opinions:
            lines.append("これまでに形成した意見:")
            for o in opinions[-MAX_OPINIONS:]:
                lines.append(f"- {o.get('topic', '')}: {o.get('opinion', '')}")
        if jokes:
            lines.append("内輪ネタ: " + ", ".join(jokes[-MAX_RUNNING_JOKES:]))
        if threads:
            lines.append("機会があれば自然に触れてもいいこと: " + ", ".join(threads[-MAX_UNRESOLVED_THREADS:]))

    text = "\n".join(lines)
    if len(text) > MAX_TRAITS_PROMPT_CHARS:
        text = text[:MAX_TRAITS_PROMPT_CHARS]
    return text


# ---------------------------------------------------------------------------
# Reflection: propose a small diff based on recent turns
# ---------------------------------------------------------------------------

_REFLECT_SYSTEM_EN = """You review a recent conversation turn to update Lily's internal
relationship state toward this user. Output ONLY valid JSON, nothing else.

Rules:
- Be conservative. Most turns should produce a mostly-empty diff. Personality
  and affinity don't lurch from one exchange.
- affinity_delta: integer from -6 to 6. Base this on emotional tone (warmth,
  rudeness, vulnerability shared, conflict, humor landing) -- not topic content.
- new_opinions: 0-1 items, ONLY if she clearly formed/revealed a specific
  opinion this turn. {"topic","opinion","confidence"}. Keep "opinion" short,
  in her voice (casual, feeling-based, not analytical).
- new_running_jokes: 0-1 short phrases, only if something memorable/funny
  happened that would plausibly get referenced again.
- new_unresolved_threads: 0-1 things the user mentioned she might naturally
  circle back to later (a goal, a worry, an upcoming event).
- resolved_threads: any existing threads (given below) that are now resolved.

Output exactly this shape:
{"affinity_delta": 0, "new_opinions": [], "new_running_jokes": [], "new_unresolved_threads": [], "resolved_threads": []}
"""

_REFLECT_SYSTEM_JA = """直近の会話1ターンを見て、このユーザーに対するリリーの内部の関係性
状態を更新します。JSON以外は一切出力しないこと。

ルール:
- 保守的に。ほとんどのターンではdiffはほぼ空でいい。性格や好感度は
  1回のやり取りで急変しない。
- affinity_delta: -6〜6の整数。会話の内容ではなく感情的なトーン
  （温かさ、そっけなさ、弱さの共有、衝突、ユーモアが通じたか）を基準に。
- new_opinions: 0〜1件。このターンで明確に意見を形成・表明した場合のみ。
  {"topic","opinion","confidence"}。opinionは短く、リリーの口調
  （タメ口、感情ベース）で。
- new_running_jokes: 0〜1件。今後また参照されそうな面白い/印象的な
  出来事があった場合のみ。
- new_unresolved_threads: 0〜1件。ユーザーが言及した、後で自然に
  触れそうなこと（目標、悩み、今後の予定）。
- resolved_threads: 下に示す既存のthreadsの中で、もう解決済みのもの。

以下の形式のみを出力:
{"affinity_delta": 0, "new_opinions": [], "new_running_jokes": [], "new_unresolved_threads": [], "resolved_threads": []}
"""


def _safe_parse_diff(raw: str) -> Optional[Dict[str, Any]]:
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    out = {
        "affinity_delta": 0,
        "new_opinions": [],
        "new_running_jokes": [],
        "new_unresolved_threads": [],
        "resolved_threads": [],
    }
    try:
        delta = int(data.get("affinity_delta", 0))
        out["affinity_delta"] = max(-MAX_AFFINITY_STEP_PER_REFLECTION,
                                     min(MAX_AFFINITY_STEP_PER_REFLECTION, delta))
    except (TypeError, ValueError):
        pass

    for op in data.get("new_opinions", []) or []:
        if isinstance(op, dict) and op.get("topic") and op.get("opinion"):
            conf = op.get("confidence", 0.6)
            out["new_opinions"].append({
                "topic": str(op["topic"])[:120],
                "opinion": str(op["opinion"])[:200],
                "confidence": float(conf) if isinstance(conf, (int, float)) else 0.6,
            })

    for j in data.get("new_running_jokes", []) or []:
        if isinstance(j, str) and j.strip():
            out["new_running_jokes"].append(j.strip()[:120])

    for th in data.get("new_unresolved_threads", []) or []:
        if isinstance(th, str) and th.strip():
            out["new_unresolved_threads"].append(th.strip()[:150])

    for th in data.get("resolved_threads", []) or []:
        if isinstance(th, str):
            out["resolved_threads"].append(th)

    return out


def _merge_diff(traits: Dict[str, Any], diff: Dict[str, Any]) -> None:
    today = time.strftime("%Y-%m-%d")

    traits["affinity"] = max(AFFINITY_MIN, min(AFFINITY_MAX,
                              traits.get("affinity", AFFINITY_DEFAULT) + diff["affinity_delta"]))

    opinions = traits.setdefault("formed_opinions", [])
    for new_op in diff["new_opinions"]:
        existing = next((o for o in opinions
                          if o.get("topic", "").lower() == new_op["topic"].lower()), None)
        if existing:
            existing["opinion"] = new_op["opinion"]
            existing["confidence"] = min(1.0, existing.get("confidence", 0.6) + 0.1)
            existing["last_touched"] = today
        else:
            opinions.append({
                "topic": new_op["topic"],
                "opinion": new_op["opinion"],
                "confidence": new_op["confidence"],
                "formed": today,
                "last_touched": today,
            })
    if len(opinions) > MAX_OPINIONS:
        opinions.sort(key=lambda o: (o.get("confidence", 0), o.get("last_touched", "")))
        traits["formed_opinions"] = opinions[-MAX_OPINIONS:]

    jokes = traits.setdefault("running_jokes", [])
    for j in diff["new_running_jokes"]:
        if j not in jokes:
            jokes.append(j)
    traits["running_jokes"] = jokes[-MAX_RUNNING_JOKES:]

    threads = traits.setdefault("unresolved_threads", [])
    for th in diff["new_unresolved_threads"]:
        if th not in threads:
            threads.append(th)
    for th in diff["resolved_threads"]:
        threads = [t for t in threads if t != th]
    traits["unresolved_threads"] = threads[-MAX_UNRESOLVED_THREADS:]

    traits["turns_since_reflection"] = 0


async def _call_reflection_llm(system_prompt: str, user_input: str, ai_output: str,
                                traits: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    context = (
        f"Current affinity: {traits.get('affinity')}\n"
        f"Current opinions: {traits.get('formed_opinions')}\n"
        f"Current unresolved threads: {traits.get('unresolved_threads')}\n\n"
        f"This turn:\nUser: {user_input}\nLily: {ai_output}"
    )
    try:
        resp = await asyncio.to_thread(
            requests.post,
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": LLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": context},
                ],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 200},
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"[Traits] reflection call failed: {e}")
        return None

    diff = _safe_parse_diff(raw)
    if diff is None and raw and raw not in ("[]", "{}"):
        print(f"[Traits] non-JSON reflection output, discarding: {raw[:200]!r}")
    return diff


async def maybe_reflect_traits(mem: dict, user_input: str, ai_output: str, language: str = "ja"):
    """Background task -- call via asyncio.create_task(), never await
    inline in the main response path (same rule as update_user_profile).
    Increments a turn counter every call; only actually fires the LLM
    reflection call every REFLECTION_EVERY_N_TURNS turns. Persists via
    save_memory(mem), same as profile_learning."""
    if mem is None or not isinstance(mem, dict):
        return
    if not user_input or not ai_output:
        return

    traits = _get_traits(mem)
    traits["turns_since_reflection"] = traits.get("turns_since_reflection", 0) + 1

    if traits["turns_since_reflection"] < REFLECTION_EVERY_N_TURNS:
        return  # not time yet -- counter increment alone doesn't need a save;
                 # it'll get persisted next time save_memory(mem) runs for any reason

    system_prompt = _REFLECT_SYSTEM_JA if language != "en" else _REFLECT_SYSTEM_EN
    diff = await _call_reflection_llm(system_prompt, user_input, ai_output, traits)
    if diff is None:
        traits["turns_since_reflection"] = 0  # don't retry every single turn on repeated failure
        return

    _merge_diff(traits, diff)
    print(f"[Traits] reflection pass complete: {diff}")

    try:
        from lily_bot import save_memory  # local import to avoid circularity at module load
        await save_memory(mem)
    except Exception as e:
        print(f"[Profile] save_memory failed (facts kept in-memory only this run): {e}")

