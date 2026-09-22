# orm/graph.py
# Copyright (C) 2005-2025 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

"""Graph access budget support for ORM eager loaders.

Defines :class:`.GraphBudget`, which bounds the amount of SQL work and the
number of distinct object identities that an eagerly loaded object graph may
consume within a single :class:`_orm.Session` / :class:`_asyncio.AsyncSession`
execution.

"""

from __future__ import annotations

from typing import Any
from typing import Optional

from .exc import GraphBudgetExceeded


class GraphBudget:
    """Configurable budget for eager-loaded object graph traversal.

    A budget constrains both the **SQL** and the **object identity** side of
    an eagerly loaded graph:

    * ``max_statements`` bounds the number of additional SELECT statements
      emitted by post-load eager loaders (i.e. the batches emitted by
      :func:`_orm.selectinload`, including each recursive level).
    * ``max_parameters`` bounds the cumulative number of bound parameters
      emitted across those statements; it is also enforced for each
      individual statement, so a single ``SELECT IN`` cannot exceed the
      database's parameter limit.
    * ``max_identities`` bounds the number of **distinct** identity-map keys
      materialized during the load.  Entities that are already present in
      the :class:`.Session`, or which are revisited through a cyclic or
      otherwise repeated relationship path, are only counted once, so cycles
      cannot exhaust the budget.

    The budget is provided to a statement or session through the
    ``orm_graph_budget`` execution option::

        budget = GraphBudget(
            max_statements=10, max_parameters=2000, max_identities=500
        )
        session.execute(
            select(Parent).options(selectinload(Parent.children)),
            execution_options={"orm_graph_budget": budget},
        )

    Counters are cumulative and may be inspected after a successful (or
    partially completed) load through :attr:`statement_count`,
    :attr:`parameter_count` and :attr:`identity_count`.  The same budget
    object may be reused for additional loads after :meth:`reset` is called.

    Thread-safety: a budget is not shared between concurrent executions by
    the ORM.  To run concurrent loads, give each execution its own
    :class:`GraphBudget` instance.

    :param max_statements: maximum number of post-load SELECT statements.
     ``None`` means unlimited.
    :param max_parameters: maximum cumulative (and per-statement) number of
     bound parameters.  ``None`` means unlimited.
    :param max_identities: maximum number of distinct materialized entity
     identities.  ``None`` means unlimited.

    .. versionadded:: 2.0.44

    """

    __slots__ = (
        "max_statements",
        "max_parameters",
        "max_identities",
        "statement_count",
        "parameter_count",
        "identity_count",
        "_identities",
    )

    def __init__(
        self,
        *,
        max_statements: Optional[int] = None,
        max_parameters: Optional[int] = None,
        max_identities: Optional[int] = None,
    ):
        self._validate_limit("max_statements", max_statements)
        self._validate_limit("max_parameters", max_parameters)
        self._validate_limit("max_identities", max_identities)
        self.max_statements = max_statements
        self.max_parameters = max_parameters
        self.max_identities = max_identities
        self.statement_count = 0
        self.parameter_count = 0
        self.identity_count = 0
        self._identities: set[Any] = set()

    @staticmethod
    def _validate_limit(name: str, value: Optional[int]) -> None:
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ValueError(f"{name} must be a non-negative integer or None")

    def reset(self) -> None:
        """Reset all counters, keeping the configured limits."""
        self.statement_count = 0
        self.parameter_count = 0
        self.identity_count = 0
        self._identities.clear()

    def charge_statement(self, num_parameters: int) -> None:
        """Account for one post-load statement with ``num_parameters`` binds.

        Raises :class:`.GraphBudgetExceeded` *before* the statement would be
        emitted if either the per-statement or cumulative parameter limit, or
        the statement limit, would be exceeded.

        """
        if (
            self.max_statements is not None
            and self.statement_count + 1 > self.max_statements
        ):
            raise GraphBudgetExceeded(
                "Graph budget exceeded: statement count %d would exceed "
                "max_statements=%d"
                % (self.statement_count + 1, self.max_statements)
            )
        if self.max_parameters is not None:
            if num_parameters > self.max_parameters:
                raise GraphBudgetExceeded(
                    "Graph budget exceeded: single statement uses %d bound "
                    "parameters, exceeding max_parameters=%d"
                    % (num_parameters, self.max_parameters)
                )
            if self.parameter_count + num_parameters > self.max_parameters:
                raise GraphBudgetExceeded(
                    "Graph budget exceeded: cumulative parameter count %d "
                    "would exceed max_parameters=%d"
                    % (
                        self.parameter_count + num_parameters,
                        self.max_parameters,
                    )
                )
        self.statement_count += 1
        self.parameter_count += num_parameters

    def charge_identity(self, identity_key: Any) -> None:
        """Account for one newly materialized entity identity.

        Distinct ``identity_key`` values (including the mapper and identity
        token) are charged only once, so cyclic and repeated relationship
        paths that resolve to the same ORM instance do not consume additional
        budget.  Raised *before* the entity is materialized when the limit is
        reached.

        """
        if identity_key in self._identities:
            return
        if (
            self.max_identities is not None
            and self.identity_count + 1 > self.max_identities
        ):
            raise GraphBudgetExceeded(
                "Graph budget exceeded: distinct identity count %d would "
                "exceed max_identities=%d"
                % (self.identity_count + 1, self.max_identities)
            )
        self._identities.add(identity_key)
        self.identity_count += 1

    def __repr__(self) -> str:
        return (
            "GraphBudget(max_statements=%r, max_parameters=%r, "
            "max_identities=%r, statements=%d, parameters=%d, "
            "identities=%d)"
            % (
                self.max_statements,
                self.max_parameters,
                self.max_identities,
                self.statement_count,
                self.parameter_count,
                self.identity_count,
            )
        )
