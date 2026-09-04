"""A questionnaire you can walk back through.

Fifteen questions with no way back turn one typo on the first step into
fifteen answers retyped. Each step can answer BACK; steps that do not apply
to the current answers are skipped in both directions; and before anything
is written the operator sees every answer and may change any one of them.
"""

from dataclasses import dataclass
from typing import Any, Callable, Optional

BACK = object()
CANCEL = object()
BACK_TOKENS = ("<", "..", "back")


@dataclass(frozen=True)
class Step:
    key: str
    ask: Callable[[dict], Any]  # returns a value, BACK or CANCEL
    applies: Callable[[dict], bool] = lambda state: True
    label: str = ""


class Cancelled(Exception):
    pass


def run_wizard(steps: list, state: Optional[dict] = None, review=None) -> dict:
    """Ask every applicable step in order; BACK returns to the previous one.

    ``review(state) -> None | key`` runs after the last step: None confirms,
    a key re-asks that single step and returns to the review.
    """
    state = {} if state is None else dict(state)
    index = 0

    while index < len(steps):
        step = steps[index]
        if not step.applies(state):
            index += 1
            continue
        answer = step.ask(state)
        if answer is CANCEL:
            raise Cancelled()
        if answer is BACK:
            index = previous_applicable(steps, state, index)
            continue
        state[step.key] = answer
        index += 1

    while review is not None:
        chosen = review(state)
        if chosen is None:
            break
        if chosen is CANCEL:
            raise Cancelled()
        step = next(s for s in steps if s.key == chosen)
        answer = step.ask(state)
        if answer is CANCEL:
            raise Cancelled()
        if answer is not BACK:
            state[step.key] = answer

    return state


def previous_applicable(steps: list, state: dict, index: int) -> int:
    candidate = index - 1
    while candidate >= 0 and not steps[candidate].applies(state):
        candidate -= 1
    return max(candidate, 0)


def is_back(text: Optional[str]) -> bool:
    return text is not None and text.strip().lower() in BACK_TOKENS
