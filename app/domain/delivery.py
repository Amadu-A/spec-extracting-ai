# app/domain/delivery.py
"""Резерв публикации одного поколения задания."""

from dataclasses import dataclass

LEGACY_DELIVERY = -1


@dataclass(frozen=True)
class DeliveryClaim:
    """Токен отличает повторную резервацию после сбоя от прежнего publisher."""

    job_id: str
    revision: int
    token: str
    attempt: int
    recovering: bool
