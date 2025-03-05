# Copyright 2025 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Macros used within all the Slurm charms."""

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeAlias

import ops

LIBAPI = 0
LIBPATCH = 1
LIBID = "ABC123"

_logger = logging.getLogger(__name__)

ConditionEvaluation: TypeAlias = tuple[bool, str]
Condition: TypeAlias = Callable[[ops.CharmBase], ConditionEvaluation]


class StopCharm(Exception):  # noqa N818
    """Exception raised to set high-priority status message."""

    @property
    def status(self) -> ops.StatusBase:
        """Get charm status passed as first argument to this exception."""
        return self.args[0]


def leader(func: Callable[..., Any]) -> Callable[..., Any]:
    """Only run method if the unit is the application leader, otherwise skip."""

    @wraps(func)
    def wrapper(charm, *args: Any, **kwargs: Any) -> Any:
        if not charm.unit.is_leader():
            return None

        return func(charm, *args, **kwargs)

    return wrapper


def integration_exists(name: str) -> Condition:
    """Check if an integration exists.

    Args:
        name: Name of integration to check existence of.
    """

    def wrapper(charm: ops.CharmBase) -> ConditionEvaluation:
        return bool(charm.model.relations[name]), ""

    return wrapper


def integration_not_exists(name: str) -> Condition:
    """Check if an integration does not exist.

    Args:
        name: Name of integration to check existence of.
    """

    def wrapper(charm: ops.CharmBase) -> ConditionEvaluation:
        not_exists = not bool(charm.model.relations[name])
        return not_exists, f"Waiting for integrations: [`{name}`]" if not_exists else ""

    return wrapper


def wait_when(*conditions: Condition) -> Callable[..., Any]:
    """Evaluate awaitable conditions.

    If a condition is `True`, set a `WaitingStatus` message.

    Args:
        *conditions: Conditions to evaluate.
    """

    def decorator(func: Callable[..., Any]):
        @wraps(func)
        def wrapper(charm: ops.CharmBase, *args: ops.EventBase, **kwargs: Any) -> Any:
            event, *_ = args
            _logger.debug("handling event `%s` on unit %s", event, charm.unit.name)

            for condition in conditions:
                result, msg = condition(charm)
                if result:
                    event.defer()
                    raise StopCharm(ops.WaitingStatus(msg))

            return func(charm, *args, **kwargs)

        return wrapper

    return decorator


def block_when(*conditions: Condition) -> Callable[..., Any]:
    """Evaluate blocking conditions.

    If a condition is `True`, set a `BlockedStatus` message.

    Args:
        *conditions: Conditions to evaluate.
    """

    def decorator(func: Callable[..., Any]):
        @wraps(func)
        def wrapper(charm: ops.CharmBase, *args: ops.EventBase, **kwargs: Any) -> Any:
            event, *_ = args
            _logger.debug("handling event `%s` on %s", event, charm.unit.name)

            for condition in conditions:
                result, msg = condition(charm)
                if result:
                    event.defer()
                    raise StopCharm(ops.BlockedStatus(msg))

            return func(charm, *args, **kwargs)

        return wrapper

    return decorator


def refresh(check: Callable[[ops.CharmBase], ops.StatusBase] | None = None) -> Callable[[Callable[[ops.CharmBase], ops.StatusBase]], None]:
    """Refresh charm state after running an event handler.

    Args:
        check: Optional condition check to run after running method.
    """

    def decorator(func: Callable[..., None]):

        @wraps(func)
        def wrapper(charm: ops.CharmBase, *args: ops.EventBase, **kwargs: Any) -> None:
            event, *_ = args
            _logger.debug(
                "refreshing state of %s after handling event `%s`", charm.unit.name, event
            )

            try:
                func(charm, *args, **kwargs)
            except StopCharm as e:
                _logger.debug(
                    "`StopCharm` raised on %s. setting status to `%s`", charm.unit.name, e.status
                )
                charm.unit.status = e.status
                return

            if check:
                charm.unit.status = check(charm)

        return wrapper

    return decorator

