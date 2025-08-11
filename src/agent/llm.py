from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Any, Dict, List, Tuple

from openai import OpenAI


class LLMClient:
    def __init__(self, api_key: str, model: str, base_url: Optional[str] = None, timeout_sec: Optional[float] = 45.0, logs_dir: Optional[Path] = None) -> None:
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.timeout_sec = timeout_sec
        self.logs_dir = logs_dir
        self.logger = logging.getLogger("agent.llm")

    def _is_gpt5(self) -> bool:
        model_name = (self.model or "").lower()
        return model_name.startswith("gpt-5") or "gpt5" in model_name or model_name.endswith("-5")

    def _token_params(self, max_tokens: int) -> Dict[str, Any]:
        """Return the appropriate token parameter for the active model.
        Some providers/models (e.g., GPT-5) reject 'max_tokens' and require 'max_completion_tokens'.
        """
        if self._is_gpt5():
            # Use provider-specific param expected by GPT-5 endpoints
            return {"extra_body": {"max_completion_tokens": max_tokens}}
        return {"max_tokens": max_tokens}

    def _temperature_params(self, temperature: float) -> Dict[str, Any]:
        """Return temperature kwargs respecting model constraints.
        Some models (e.g., GPT-5) only support the default (1) and may reject explicit values.
        For such models, omit the parameter entirely.
        """
        if self._is_gpt5():
            return {}
        return {"temperature": temperature}

    def _log_llm_interaction(self, method: str, system_prompt: str, user_prompt: str, response: str, extra_data: Optional[Dict[str, Any]] = None) -> None:
        """Log LLM interactions to both general logger and dedicated LLM log files."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]  # Include milliseconds
        
        # Log to general logger
        self.logger.info(f"LLM {method} call - Model: {self.model}")
        
        # Log to dedicated files if logs_dir is available
        if self.logs_dir:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            
            # Create structured log entry
            log_entry = {
                "timestamp": timestamp,
                "method": method,
                "model": self.model,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response": response,
                "extra_data": extra_data or {}
            }
            
            # Write to timestamped JSON file
            json_log_path = self.logs_dir / f"llm_interaction_{timestamp}.json"
            with open(json_log_path, 'w', encoding='utf-8') as f:
                json.dump(log_entry, f, indent=2, ensure_ascii=False)
            
            # Also append to a consolidated log file for easy reading
            consolidated_log_path = self.logs_dir / "llm_interactions.log"
            with open(consolidated_log_path, 'a', encoding='utf-8') as f:
                f.write(f"\n{'='*80}\n")
                f.write(f"TIMESTAMP: {timestamp}\n")
                f.write(f"METHOD: {method}\n")
                f.write(f"MODEL: {self.model}\n")
                f.write(f"SYSTEM PROMPT:\n{system_prompt}\n")
                f.write(f"USER PROMPT:\n{user_prompt}\n")
                f.write(f"RESPONSE:\n{response}\n")
                if extra_data:
                    f.write(f"EXTRA DATA: {json.dumps(extra_data, indent=2)}\n")
                f.write(f"{'='*80}\n")

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.2, max_tokens: int = 1024) -> str:
        token_kwargs = self._token_params(max_tokens)
        temp_kwargs = self._temperature_params(temperature)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            **token_kwargs,
            **temp_kwargs,
            timeout=self.timeout_sec,
        )
        response_content = response.choices[0].message.content or ""
        
        # Log the interaction
        extra_data = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "usage": response.usage.model_dump() if response.usage else None
        }
        self._log_llm_interaction("complete", system_prompt, user_prompt, response_content, extra_data)
        
        return response_content

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
        token_kwargs = self._token_params(max_tokens)
        temp_kwargs = self._temperature_params(0.2)
        content = ""
        usage_data = None
        fallback_used = False
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                **token_kwargs,
                **temp_kwargs,
                timeout=self.timeout_sec,
            )
            content = response.choices[0].message.content or "{}"
            usage_data = response.usage.model_dump() if response.usage else None
        except Exception as e:
            # Fallback: ask for raw JSON in plain text
            fallback_used = True
            plain_system = system_prompt + "\nReturn ONLY JSON."
            content = self.complete(plain_system, user_prompt, max_tokens=max_tokens)
        
        import json as _json
        parsed_json = {}
        parse_error = None
        
        try:
            parsed_json = _json.loads(content)
        except Exception as e:
            parse_error = str(e)
            # Fallback: try to extract the first JSON object substring
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", content)
            if m:
                try:
                    parsed_json = _json.loads(m.group(0))
                except Exception:
                    parsed_json = {}
            else:
                parsed_json = {}
        
        # Log the interaction
        extra_data = {
            "max_tokens": max_tokens,
            "usage": usage_data,
            "fallback_used": fallback_used,
            "parse_error": parse_error,
            "raw_content": content,
            "parsed_result": parsed_json
        }
        self._log_llm_interaction("complete_json", system_prompt, user_prompt, json.dumps(parsed_json, indent=2), extra_data)
        
        return parsed_json

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
        token_kwargs = self._token_params(max_tokens)
        temp_kwargs = self._temperature_params(0.2)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=tools,
            tool_choice=tool_choice,
            **token_kwargs,
            **temp_kwargs,
            timeout=self.timeout_sec,
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
        
        # Log the interaction
        response_content = msg.content or ""
        extra_data = {
            "max_tokens": max_tokens,
            "tool_choice": tool_choice,
            "tools_available": [tool.get("function", {}).get("name", "unknown") for tool in tools],
            "tool_calls": simple_tool_calls,
            "usage": response.usage.model_dump() if response.usage else None
        }
        self._log_llm_interaction("chat_with_tools", system_prompt, user_prompt, response_content, extra_data)
        
        return {"content": response_content, "role": msg.role}, simple_tool_calls


