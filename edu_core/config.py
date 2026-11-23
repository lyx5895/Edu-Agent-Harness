"""Runtime settings, read from the environment. The API key is never stored in code or files."""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Development defaults (design doc 8.2). Every role can be overridden with EDU_MODEL_<ROLE>.
DEFAULT_MODELS = {
    'generator': 'DeepSeek-V4.1-Flash',
    'grader': 'DeepSeek-V4.1-Flash',
    'analyst': 'DeepSeek-V4.1-Flash',
    # Another provider than the generator (blind review). Sonnet replaced qwen3.8-flash on 2026-11-20: median 14 s
    # against 30 s on the same reviews, no 100 s+ tail, and closer to the reference reviewer (TEACHER_AGENT_DESIGN.md).
    'reviewer': 'claude-sonnet-5-5',
    # Transcribes photos of student work; must accept image input. DeepSeek-V4.1-Flash fails here on the
    # gateway (image + system prompt + json_schema -> bad_alloc; without a schema it thinks past max_tokens).
    'reader': 'gemini-3.8-flash',
}


@dataclass(frozen=True)
class Settings:
    base_url: str = 'https://lightingtheword.com/v1'
    api_key: str = ''
    models: dict = field(default_factory=lambda: dict(DEFAULT_MODELS))
    db: str = str(ROOT / 'data/edu.sqlite')
    # Share of fully-earned gradings re-checked by the reviewer to monitor false passes.
    audit_rate: float = 0.05
    # Chance that one objective also gets an independent second version for an A/B comparison. The two arms share
    # the task input and differ only in the system prompt; the comparison is per objective, not per question.
    ab_rate: float = 0.5
    # Write the self-check and the rewrite it may call for after the questions are returned, so the teacher is not
    # held up by work that only adds notes to questions they can already read. The A/B versions come with the
    # questions: the teacher compares them while reviewing.
    background_checks: bool = False
    # Reasoning models spend output tokens on thinking; a low cap returns empty content.
    max_tokens: int = 6000
    timeout: float = 180.0
    concurrency: int = 8
    # Single-user local tool without accounts: only loopback clients unless explicitly opened up.
    allow_remote: bool = False
    # Accounts and per-record access checks (design doc 10). Off: local single-user tool, as before.
    auth: bool = False


def load_settings(**overrides) -> Settings:
    models = {role: os.getenv(f'EDU_MODEL_{role.upper()}', model) for role, model in DEFAULT_MODELS.items()}
    values = {
        'base_url': os.getenv('EDU_LLM_BASE_URL', Settings.base_url),
        'api_key': os.getenv('EDU_LLM_API_KEY', ''),
        'models': models,
        'db': os.getenv('EDU_DB', Settings.db),
        'audit_rate': float(os.getenv('EDU_AUDIT_RATE', Settings.audit_rate)),
        'ab_rate': float(os.getenv('EDU_AB_RATE', Settings.ab_rate)),
        'background_checks': os.getenv('EDU_BACKGROUND_CHECKS', '') in ('1', 'true', 'yes'),
        'max_tokens': int(os.getenv('EDU_MAX_TOKENS', Settings.max_tokens)),
        'concurrency': int(os.getenv('EDU_CONCURRENCY', Settings.concurrency)),
        'allow_remote': os.getenv('EDU_ALLOW_REMOTE', '') in ('1', 'true', 'yes'),
        'auth': os.getenv('EDU_AUTH', '') in ('1', 'true', 'yes'),
    }
    values.update(overrides)
    return Settings(**values)
