"""AI orchestration logic for planning crash analysis."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openai import OpenAI, AzureOpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from .config import AppConfig


LOG = logging.getLogger(__name__)


@dataclass
class CrashCommandPlan:
    """Instructions produced by the AI for crash analysis."""

    reasoning: str
    commands: List[str] = field(default_factory=list)
    verdict: Optional[str] = None
    references: List[str] = field(default_factory=list)


class AIOrchestrator:
    """High-level AI driver to interact with OpenAI models."""

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        self.config = config or AppConfig()
        self._use_azure = self.config.azure_enabled()

        if self._use_azure:
            self._api_key = self.config.azure_openai_api_key or self.config.openai_api_key
            if not self._api_key:
                raise ValueError(
                    "AZURE_OPENAI_API_KEY must be set when Azure OpenAI is enabled"
                )
        else:
            self._api_key = self.config.openai_api_key
            if not self._api_key:
                LOG.warning("OPENAI_API_KEY not set; AI orchestration will be disabled.")

        if self._use_azure:
            if not self.config.azure_openai_endpoint:
                raise ValueError("AZURE_OPENAI_ENDPOINT must be set when using Azure OpenAI")
            if not self.config.azure_openai_deployment:
                raise ValueError("AZURE_OPENAI_DEPLOYMENT must be set when using Azure OpenAI")
            self.client = AzureOpenAI(
                api_key=self._api_key,
                api_version=self.config.azure_openai_api_version,
                azure_endpoint=self.config.azure_openai_endpoint,
            )
            self._model_name = self.config.azure_openai_deployment
            LOG.info("Using Azure OpenAI deployment %s", self._model_name)
        else:
            self.client = OpenAI(
                api_key=self._api_key,
                base_url=self.config.openai_base_url,
            )
            self._model_name = self.config.openai_model

        self._system_prompt = "\n".join(
            [
                "You are a world-class Linux kernel crash analysis expert.",
                "You are working with the `crash` utility against a vmcore dump.",
                "Only suggest safe read-only commands; never modify VM state.",
                "Respond in strict JSON with fields reasoning, commands, verdict, references.",
                "Reasoning should be concise (<= 120 words).",
                "Commands must be an array of crash CLI commands to run next.",
                "Leave verdict null until you are confident about root cause.",
                "References must list only verified CVEs, bug IDs, or URLs that you are certain exist.",
                "If you cannot cite a trustworthy reference, return an empty references array instead of guessing.",
                "Prefer references sourced from the provided knowledge base notes when applicable.",
            ]
        )

    # ------------------------------------------------------------------
    @retry(
        reraise=True,
        wait=wait_fixed(2),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    def _call_openai(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        LOG.debug("Dispatching %d messages to OpenAI", len(messages))
        completion = self.client.chat.completions.create(
            model=self._model_name,
            response_format={"type": "json_object"},
            messages=messages,
        )
        content = completion.choices[0].message.content
        if not content:
            raise RuntimeError("Empty response from OpenAI")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:  # pragma: no cover - depends on model output
            LOG.error("Failed to decode JSON payload: %s", content)
            raise RuntimeError("Invalid JSON returned by OpenAI") from exc
        return payload

    # ------------------------------------------------------------------
    def plan_next_commands(
        self,
        crash_transcript: str,
        prior_commands: List[str],
        knowledge_summaries: Optional[List[str]] = None,
    ) -> CrashCommandPlan:
        """Ask the AI for the next crash commands based on transcript."""

        context_segments = [
            "Latest crash transcript:",
            crash_transcript or "(no output yet)",
        ]
        if prior_commands:
            context_segments.append(
                "Commands executed so far: " + ", ".join(prior_commands)
            )
        if knowledge_summaries:
            context_segments.append(
                "Relevant knowledge base notes:\n" + "\n".join(knowledge_summaries)
            )

        user_prompt = "\n\n".join(context_segments)
        payload = self._call_openai(
            [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )

        plan = CrashCommandPlan(
            reasoning=payload.get("reasoning", ""),
            commands=payload.get("commands", []),
            verdict=payload.get("verdict"),
            references=payload.get("references", []),
        )
        LOG.debug("AI proposed %d commands", len(plan.commands))
        return plan

    # ------------------------------------------------------------------
    def craft_final_report(
        self,
        crash_transcript: str,
        knowledge_matches: List[str],
        hypothesis: str,
    ) -> str:
        """Generate a polished summary referencing potential fixes."""

        user_prompt = "\n".join(
            [
                "Produce a detailed crash analysis summary.",
                f"Working hypothesis: {hypothesis}",
                "Crash investigation transcript:",
                crash_transcript,
                "References to include:",
                "\n".join(knowledge_matches) if knowledge_matches else "(none)",
                "Highlight likely root cause and remediation/patch guidance.",
            ]
        )

        payload = self._call_openai(
            [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        return json.dumps(payload, indent=2)


__all__ = ["AIOrchestrator", "CrashCommandPlan"]
