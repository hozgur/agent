from __future__ import annotations

from typing import Iterable, Optional, Any, Dict, List, Tuple

from openai import OpenAI


class LLMClient:
    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None) -> None:
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.2, max_tokens: int = 1024) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    def summarize_chunks(self, system_prompt: str, chunks: Iterable[str], max_tokens: int = 512) -> str:
        partials = []
        for idx, chunk in enumerate(chunks, start=1):
            content = f"Chunk {idx}:\n{chunk[:8000]}"
            summary = self.complete(system_prompt, content, max_tokens=max_tokens)
            partials.append(summary)
        merged = "\n\n".join(partials)
        final_summary = self.complete(system_prompt, f"Merge and deduplicate:\n{merged}")
        return final_summary

    def complete_json(self, system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> Dict[str, Any]:
        """
        Ask the model to return a single JSON object. Uses JSON mode to enforce valid JSON.
        NOTE: Schema is described in the prompt; we rely on json_object mode for compatibility.
        """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        import json as _json
        try:
            return _json.loads(content)
        except Exception:
            # Fallback: try to extract the first JSON object substring
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", content)
            if m:
                return _json.loads(m.group(0))
            return {}

    def chat_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: List[Dict[str, Any]],
        tool_choice: str = "auto",
        max_tokens: int = 1024,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Call the chat completion API with function/tool calling enabled. Returns the assistant message and any tool calls.
        """
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=tools,
            tool_choice=tool_choice,
            temperature=0.2,
            max_tokens=max_tokens,
        )
        msg = response.choices[0].message
        tool_calls = msg.tool_calls or []
        # Convert tool_calls objects to simple dicts
        simple_tool_calls: List[Dict[str, Any]] = []
        for tc in tool_calls:
            simple_tool_calls.append({
                "id": tc.id,
                "type": tc.type,
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            })
        return {"content": msg.content or "", "role": msg.role}, simple_tool_calls


