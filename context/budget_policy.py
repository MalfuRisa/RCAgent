"""Separate an efficiency target from a finite engineering safety ceiling.

These are serialized-request characters, not model tokens. The independent
ceiling is a local operational safeguard until endpoint token limits are wired
in. Strict mode preserves the earlier contract for rollback and comparison.
"""
from config import settings


def hard_limit():
    if settings.CONTEXT_BUDGET_POLICY == 'strict':
        return settings.CONTEXT_MAX_CHARS
    return settings.CONTEXT_HARD_MAX_CHARS


def can_expand():
    return settings.CONTEXT_BUDGET_POLICY == 'soft' and hard_limit() > settings.CONTEXT_MAX_CHARS


def audit(request_chars):
    return {'budget_policy': settings.CONTEXT_BUDGET_POLICY,
            'soft_limit_chars': settings.CONTEXT_MAX_CHARS,
            'hard_limit_chars': hard_limit(),
            'soft_target_exceeded': request_chars > settings.CONTEXT_MAX_CHARS,
            'engineering_ceiling_exceeded': request_chars > hard_limit(),
            'capacity_unit': 'serialized_request_characters_not_model_tokens'}
