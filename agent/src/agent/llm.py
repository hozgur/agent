from __future__ import annotations

from typing import Iterable, Optional

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


