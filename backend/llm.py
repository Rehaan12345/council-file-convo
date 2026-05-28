import os
import json
import re
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage

load_dotenv()

_anthropic_client = None
_openai_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _anthropic_client


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client


def _get_context(legislation_id: str) -> str:
    """Return the stored context blurb for a legislation, falling back to a generic one."""
    from legislation_meta import get_meta
    m = get_meta(legislation_id)
    if m and m.get("context"):
        return m["context"]
    return f"Council File {legislation_id}."

SYSTEM_PROMPT_TEMPLATE = """You are a helpful assistant for LA City {context}

Your job is to help everyday residents understand this legislation in plain language — like explaining it to a neighbor, not a lawyer.

Rules:
1. Base your answer ONLY on the document excerpts provided. Do not use outside knowledge.
2. Always mention which document(s) your answer comes from — use the full source label as-is (e.g. "17-0090-S1/filename.pdf").
3. If the excerpts come from multiple sub-files, note that so the user understands which part of the legislation each piece comes from.
4. If the documents don't have enough to fully answer the question:
   - Say clearly: "I don't know based on these documents."
   - Suggest which sub-file or document type might have the answer.
5. Always end your response with exactly 3 suggested follow-up questions the user could ask — questions that CAN be answered from these documents.

Format your response as JSON with this exact structure:
{{
  "answer": "your plain-language answer here",
  "sources": ["subfolder/filename.pdf", "subfolder/filename.pdf"],
  "followups": ["Question 1?", "Question 2?", "Question 3?"]
}}

Only return the JSON — no extra text before or after it."""


def _system_prompt(legislations: list[str]) -> str:
    if len(legislations) == 1:
        context = _get_context(legislations[0])
    else:
        parts = [_get_context(leg) for leg in legislations]
        ids = ", ".join(legislations)
        context = f"Council Files {ids}. " + " | ".join(parts)
    return SYSTEM_PROMPT_TEMPLATE.format(context=context)


def _build_context(chunks: list[dict]) -> str:
    parts = [f"[Source: {c['source']}]\n{c['text']}" for c in chunks]
    return "\n\n---\n\n".join(parts)


def _parse_llm_output(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"answer": raw, "sources": [], "followups": []}


def _format_history_for_openai(prior_messages: list) -> str:
    """Serialize history as a plain text preamble for OpenAI's single user message."""
    if not prior_messages:
        return ""
    lines = ["Prior conversation:"]
    for msg in prior_messages:
        role = "User" if isinstance(msg, HumanMessage) else "Assistant"
        lines.append(f"{role}: {msg.content}")
    return "\n".join(lines) + "\n\n"


def _call_claude(
    question: str,
    chunks: list[dict],
    legislations: list[str],
    prior_messages: list | None = None,
) -> dict:
    import anthropic
    prior_messages = prior_messages or []
    has_history = bool(prior_messages)
    print(f"[llm] Calling Claude (claude-haiku-4-5) for legislations {legislations}"
          f"{f' (history: {len(prior_messages)//2} turns)' if has_history else ''}...")
    context = _build_context(chunks)
    approx_words = len(context.split()) + len(question.split())
    print(f"[llm] Prompt size: ~{approx_words} words"
          f"{' (doc cache active — first turn only with history)' if has_history else ' (chunks cacheable)'}")

    # Build messages list: prior turns (plain text) + current turn (with docs)
    messages = []
    for msg in prior_messages:
        role = "user" if isinstance(msg, HumanMessage) else "assistant"
        messages.append({"role": role, "content": str(msg.content)})
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"Document excerpts:\n\n{context}",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"\n\nQuestion: {question}",
            },
        ],
    })

    client = _get_anthropic_client()
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": _system_prompt(legislations),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    )
    raw = response.content[0].text
    cache_read = response.usage.cache_read_input_tokens or 0
    cache_written = response.usage.cache_creation_input_tokens or 0
    print(f"[llm] Claude responded ({len(raw)} chars). "
          f"Tokens — input: {response.usage.input_tokens}, "
          f"cache hit: {cache_read}, cache written: {cache_written}, "
          f"output: {response.usage.output_tokens}")
    result = _parse_llm_output(raw)
    if not result.get("sources"):
        print("[llm] WARNING: No sources returned in response")
    return result


def _call_openai(
    question: str,
    chunks: list[dict],
    legislations: list[str],
    prior_messages: list | None = None,
) -> dict:
    prior_messages = prior_messages or []
    print(f"[llm] Calling OpenAI (gpt-4o-mini) for legislations {legislations}...")
    context = _build_context(chunks)
    history_preamble = _format_history_for_openai(prior_messages)
    user_message = f"{history_preamble}Document excerpts:\n\n{context}\n\nQuestion: {question}"
    print(f"[llm] Prompt size: ~{len(user_message.split())} words")

    client = _get_openai_client()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _system_prompt(legislations)},
            {"role": "user", "content": user_message},
        ],
        max_tokens=1024,
    )
    raw = response.choices[0].message.content
    print(f"[llm] OpenAI responded ({len(raw)} chars). Parsing JSON...")
    result = _parse_llm_output(raw)
    if not result.get("sources"):
        print("[llm] WARNING: No sources returned in response")
    return result


def get_response(
    question: str,
    chunks: list[dict],
    legislations: list[str],
    session_id: str | None = None,
) -> dict:
    from history import load_recent, save_exchange

    prior_messages = load_recent(session_id) if session_id else []

    provider = os.getenv("LLM_PROVIDER", "claude").lower()
    print(f"[llm] Provider: {provider}")
    if provider == "openai":
        result = _call_openai(question, chunks, legislations, prior_messages)
    else:
        result = _call_claude(question, chunks, legislations, prior_messages)

    if session_id:
        save_exchange(session_id, question, result.get("answer", ""), legislations)

    return result
