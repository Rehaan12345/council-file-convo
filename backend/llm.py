import os
import json
import re
from dotenv import load_dotenv

load_dotenv()

# Per-legislation context injected into the system prompt
LEGISLATION_CONTEXT = {
    "17-0090": (
        "Council File 17-0090 and all of its related sub-files (S1 through S33). "
        "These documents cover affordable housing requirements, local hire initiatives, "
        "and community development in South and Southeast Los Angeles."
    ),
    "24-0011": (
        "Council File 24-0011 and all of its related sub-files (S1 through S35). "
        "These documents cover street services, supplemental tree trimming, and "
        "related Bureau of Street Services actions in Council District 3."
    ),
    "26-0900": (
        "Council File 26-0900 and all of its related sub-files. "
        "These documents cover street lighting assessment districts across multiple areas of Los Angeles. "
        "They include ordinances establishing and modifying lighting districts, property owner ballot "
        "proceedings under California's Proposition 218, Board of Public Works notifications, and "
        "weighted ballot processes that determine whether assessments are imposed on affected properties."
    ),
}

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
        context = LEGISLATION_CONTEXT.get(legislations[0], f"Council File {legislations[0]}.")
    else:
        parts = []
        for leg in legislations:
            parts.append(LEGISLATION_CONTEXT.get(leg, f"Council File {leg}"))
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


def _call_claude(question: str, chunks: list[dict], legislations: list[str]) -> dict:
    import anthropic
    print(f"[llm] Calling Claude (claude-haiku-4-5) for legislations {legislations}...")
    context = _build_context(chunks)
    approx_words = len(context.split()) + len(question.split())
    print(f"[llm] Prompt size: ~{approx_words} words (chunks cacheable, question not)")

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        system=[
            {
                "type": "text",
                "text": _system_prompt(legislations),
                "cache_control": {"type": "ephemeral"},  # cache system prompt (~350 tokens)
            }
        ],
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"Document excerpts:\n\n{context}",
                    "cache_control": {"type": "ephemeral"},  # cache chunks (~2,650 tokens)
                },
                {
                    "type": "text",
                    "text": f"\n\nQuestion: {question}",  # question is never cached
                },
            ],
        }],
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


def _call_openai(question: str, chunks: list[dict], legislations: list[str]) -> dict:
    from openai import OpenAI
    print(f"[llm] Calling OpenAI (gpt-4o-mini) for legislations {legislations}...")
    context = _build_context(chunks)
    user_message = f"Document excerpts:\n\n{context}\n\nQuestion: {question}"
    print(f"[llm] Prompt size: ~{len(user_message.split())} words")

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
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


def get_response(question: str, chunks: list[dict], legislations: list[str]) -> dict:
    provider = os.getenv("LLM_PROVIDER", "claude").lower()
    print(f"[llm] Provider: {provider}")
    if provider == "openai":
        return _call_openai(question, chunks, legislations)
    return _call_claude(question, chunks, legislations)
