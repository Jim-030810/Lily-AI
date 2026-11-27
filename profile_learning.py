"""
profile_learning.py

Lets Lily build a durable, factual profile of the user over time --
separate from the fuzzy vector-memory recall and the free-form
`summary` text. Facts extracted here are meant to be short, concrete,
and reusable (e.g. "user is job-hunting", "user prefers short direct
answers", "user has a cat named Mochi") -- NOT conversational color,
NOT opinions Lily formed, and NOT anything from her own generated
replies.

Design goals:
  - Runs as a background task (fire-and-forget) so it never adds
    latency to the reply the user actually hears.
  - Uses a plain, non-persona LLM call -- extraction accuracy matters
    more here than staying in character.
  - Deduplicates against what's already known so the profile doesn't
    balloon or repeat itself.
  - Caps total size so it stays cheap to inject into every prompt.
  - Fully separate from add_conversation/output_is_bad -- a bad or
    short reply doesn't block fact extraction, and a good reply
    doesn't guarantee any new fact (most turns yield nothing, which is
    correct and expected).

Wire-up (see bottom of file for the exact call sites):
  1. In Lily_generate_response(), inject format_profile_for_prompt(mem)
     as an extra system message, alongside the existing summary/memory
     injection.
  2. In process_response()'s NORMAL CHAT branch, after save_memory(mem),
     fire off:
         asyncio.create_task(update_user_profile(mem, user_input, cleaned, CURRENT_LANGUAGE))
     Fire-and-forget -- do NOT await it inline, or every reply gets
     slower waiting on a second LLM call for no user-facing benefit.
"""

import asyncio
import json
import re
import time
from typing import List, Dict, Any

import requests  # already a dependency in the main module

# These two are expected to already exist in the main module's global
# scope (OLLAMA_URL, LLAMA_MODEL) -- import them from there instead of
# hardcoding if this file lives alongside lily_bot.py:
#   from lily_bot import OLLAMA_URL, LLAMA_MODEL
# Left as plain names here so this file has no circular import assumptions
# baked in -- wire up however fits your project layout.
OLLAMA_URL = "http://127.0.0.1:11434"
LLAMA_MODEL = "qwen2.5:7b"

MAX_PROFILE_FACTS = 40
MAX_PROFILE_PROMPT_CHARS = 700

_EXTRACT_SYSTEM_EN = """You extract durable facts about a user from one conversation turn.
Only extract things that are:
- Concrete and reusable later (preferences, ongoing situations, life details, recurring topics, communication style)
- Actually stated or clearly implied by the user in THIS turn
- NOT already in the existing facts list below (don't repeat or rephrase an existing fact)
- NOT something the assistant said about herself
- NOT a one-off pleasantry or small talk with no lasting value

Output ONLY a JSON array of strings, 0 to 2 items. No other text, no markdown, no explanation.
If nothing new and durable was stated, output exactly: []
"""

_EXTRACT_SYSTEM_JA = """あなたは会話の1ターンから、ユーザーに関する「今後も使える事実」だけを抽出します。
抽出していいのは以下の条件を満たすものだけ：
- 今後の会話でも使える具体的な内容（好み・状況・生活の詳細・繰り返し出る話題・話し方の傾向）
- このターンでユーザーが実際に言った、または明確に示したこと
- 下にある既存の事実リストと重複しない（言い換えも禁止）
- アシスタント自身について言ったことではない
- その場限りの挨拶や雑談で、今後価値がないものではない

出力は文字列のJSON配列のみ。0〜2個。他のテキスト・マークダウン・説明は一切禁止。
新しく残すべき事実がなければ、必ずこれだけを出力： []
"""


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def _is_duplicate(candidate: str, existing: List[Dict[str, Any]]) -> bool:
    """Cheap similarity check -- exact or near-exact match against
    existing facts. Not semantic, but catches the common case of the
    model re-stating something already known in slightly different
    words within the same session."""
    cand_norm = _normalize(candidate)
    if not cand_norm:
        return True
    for f in existing:
        existing_norm = _normalize(f.get("fact", ""))
        if not existing_norm:
            continue
        if cand_norm == existing_norm:
            return True
        shorter, longer = sorted([cand_norm, existing_norm], key=len)
        if len(shorter) >= 6 and shorter in longer:
            return True
    return False


async def _call_extraction_llm(system_prompt: str, user_input: str, ai_output: str,
                                existing_facts_text: str) -> List[str]:
    prompt = (
        f"既存の事実リスト:\n{existing_facts_text or '（まだなし）'}\n\n"
        f"会話:\nユーザー: {user_input}\nアシスタント: {ai_output}\n"
    ) if system_prompt is _EXTRACT_SYSTEM_JA else (
        f"Existing facts:\n{existing_facts_text or '(none yet)'}\n\n"
        f"Conversation:\nUser: {user_input}\nAssistant: {ai_output}\n"
    )

    try:
        resp = await asyncio.to_thread(
            requests.post,
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": LLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 150},
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        print(f"[Profile] extraction call failed: {e}")
        return []

    # Model may wrap the array in markdown fences despite instructions --
    # strip those defensively before parsing.
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    except Exception:
        pass
    if raw and raw != "[]":
        print(f"[Profile] non-JSON extraction output, discarding: {raw[:200]!r}")
    return []


async def update_user_profile(mem: dict, user_input: str, ai_output: str, language: str = "ja"):
    """Background task -- call via asyncio.create_task(), never await
    inline in the main response path. Extracts 0-2 new facts and
    merges them into mem['user_profile'], then saves mem to disk.

    Caller is responsible for having a working save_memory(mem) already
    imported/available; this function calls it directly at the end so
    the profile update is durable even if the process exits shortly
    after (idle/autonomous loops, shutdown, etc).
    """
    if mem is None or not isinstance(mem, dict):
        return
    if not user_input or not ai_output:
        return

    existing = mem.setdefault("user_profile", [])
    existing_text = "\n".join(f"- {f.get('fact', '')}" for f in existing[-MAX_PROFILE_FACTS:])

    system_prompt = _EXTRACT_SYSTEM_JA if language != "en" else _EXTRACT_SYSTEM_EN
    new_facts = await _call_extraction_llm(system_prompt, user_input, ai_output, existing_text)

    added_any = False
    for fact in new_facts:
        if len(fact) < 4 or len(fact) > 200:
            continue
        if _is_duplicate(fact, existing):
            continue
        existing.append({"fact": fact, "ts": int(time.time())})
        added_any = True
        print(f"[Profile] + {fact!r}")

    if not added_any:
        return

    # Cap total size -- drop oldest first. Facts aren't weighted/scored
    # here on purpose (simplicity); if the cap starts feeling too
    # aggressive in practice, raise MAX_PROFILE_FACTS rather than adding
    # scoring logic prematurely.
    if len(existing) > MAX_PROFILE_FACTS:
        mem["user_profile"] = existing[-MAX_PROFILE_FACTS:]

    try:
        from lily_bot import save_memory  # local import to avoid circularity at module load
        await save_memory(mem)
    except Exception as e:
        print(f"[Profile] save_memory failed (facts kept in-memory only this run): {e}")


def _age_label(ts: float, language: str = "ja", now: float | None = None) -> str:
    """Rough human-readable age ('2 weeks ago' / '約2週間前') so the
    model can tell a settled, dated mention apart from something that
    just happened -- without this, every stored fact reads as equally
    fresh and equally urgent, which is what causes her to treat a
    two-week-old passing comment like breaking news."""
    now = now if now is not None else time.time()
    days = int((now - ts) // 86400)
    if language == "en":
        if days <= 0:
            return "today"
        if days == 1:
            return "1 day ago"
        if days < 14:
            return f"{days} days ago"
        if days < 60:
            return f"{days // 7} weeks ago"
        return f"{days // 30} months ago"
    else:
        if days <= 0:
            return "今日"
        if days == 1:
            return "1日前"
        if days < 14:
            return f"{days}日前"
        if days < 60:
            return f"約{days // 7}週間前"
        return f"約{days // 30}ヶ月前"


def format_profile_for_prompt(mem: dict, language: str = "ja") -> str:
    """Returns a compact block suitable for injecting as a system
    message, or '' if there's nothing to show yet. Caller decides
    whether to inject it (skip if empty).

    Each fact is tagged with its age so the model can treat old,
    one-off mentions as settled background instead of something to
    react to as if it just happened."""
    if not mem or not isinstance(mem, dict):
        return ""
    facts = mem.get("user_profile", [])
    if not facts:
        return ""
    now = time.time()
    lines = []
    for f in facts:
        fact_text = f.get("fact", "")
        if not fact_text:
            continue
        ts = f.get("ts", now)
        age = _age_label(ts, language=language, now=now)
        lines.append(f"- {fact_text} ({age})")
    text = "\n".join(lines)
    if len(text) > MAX_PROFILE_PROMPT_CHARS:
        # Keep the most recent facts if it overflows the budget --
        # recency is a reasonable proxy for relevance without adding
        # a real ranking step.
        text = "\n".join(lines[-1:-((MAX_PROFILE_PROMPT_CHARS // 40)):-1])
    return text