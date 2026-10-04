# ext/mutable.py
# Copyright (C) 2005-2026 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php

r"""Provide support for tracking of in-place changes to scalar values,
which are propagated into ORM change events on owning parent objects.

.. _mutable_scalars:

Establishing Mutability on Scalar Column Values
===============================================

A typical example of a "mutable" structure is a Python dictionary.
Following the example introduced in :ref:`types_toplevel`, we
begin with a custom type that marshals Python dictionaries into
JSON strings before being persisted::

    from sqlalchemy.types import TypeDecorator, VARCHAR
    import json


    class JSONEncodedDict(TypeDecorator):
        "Represents an immutable structure as a json-encoded string."

        impl = VARCHAR

        def process_bind_param(self, value, dialect):
            if value is not None:
                value = json.dumps(value)
            return value

        def process_result_value(self, value, dialect):
            if value is not None:
                value = json.loads(value)
            return value

The usage of ``json`` is only for the purposes of example. The
:mod:`sqlalchemy.ext.mutable` extension can be used
with any type whose target Python type may be mutable, including
:class:`.PickleType`, :class:`_postgresql.ARRAY`, etc.

When using the :mod:`sqlalchemy.ext.mutable` extension, the value itself
tracks all parents which reference it.  Below, we illustrate a simple
version of the :class:`.MutableDict` dictionary object, which applies
the :class:`.Mutable` mixin to a plain Python dictionary::

    from sqlalchemy.ext.mutable import Mutable


    class MutableDict(Mutable, dict):
        @classmethod
        def coerce(cls, key, value):
            "Convert plain dictionaries to MutableDict."

            if not isinstance(value, MutableDict):
                if isinstance(value, dict):
                    return MutableDict(value)

                # this call will raise ValueError
                return Mutable.coerce(key, value)
            else:
                return value

        def __setitem__(self, key, value):
            "Detect dictionary set events and emit change events."

            dict.__setitem__(self, key, value)
            self.changed()

        def __delitem__(self, key):
            "Detect dictionary del events and emit change events."

            dict.__delitem__(self, key)
            self.changed()

The above dictionary class takes the approach of subclassing the Python
built-in ``dict`` to produce a dict
subclass which routes all mutation events through ``__setitem__``.  There are
variants on this approach, such as subclassing ``UserDict.UserDict`` or
``collections.MutableMapping``; the part that's important to this example is
that the :meth:`.Mutable.changed` method is called whenever an in-place
change to the datastructure takes place.

We also redefine the :meth:`.Mutable.coerce` method which will be used to
convert any values that are not instances of ``MutableDict``, such
as the plain dictionaries returned by the ``json`` module, into the
appropriate type.  Defining this method is optional; we could just as well
created our ``JSONEncodedDict`` such that it always returns an instance
of ``MutableDict``, and additionally ensured that all calling code
uses ``MutableDict`` explicitly.  When :meth:`.Mutable.coerce` is not
overridden, any values applied to a parent object which are not instances
of the mutable type will raise a ``ValueError``.

Our new ``MutableDict`` type offers a class method
:meth:`~.Mutable.as_mutable` which we can use within column metadata
to associate with types. This method grabs the given type object or
class and associates a listener that will detect all future mappings
of this type, applying event listening instrumentation to the mapped
attribute. Such as, with classical table metadata::

    from sqlalchemy import Table, Column, Integer

    my_data = Table(
        "my_data",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("data", MutableDict.as_mutable(JSONEncodedDict)),
    )

Above, :meth:`~.Mutable.as_mutable` returns an instance of ``JSONEncodedDict``
(if the type object was not an instance already), which will intercept any
attributes which are mapped against this type.  Below we establish a simple
mapping against the ``my_data`` table::

    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.orm import Mapped
    from sqlalchemy.orm import mapped_column


    class Base(DeclarativeBase):
        pass


    class MyDataClass(Base):
        __tablename__ = "my_data"
        id: Mapped[int] = mapped_column(primary_key=True)
        data: Mapped[dict[str, str]] = mapped_column(
            MutableDict.as_mutable(JSONEncodedDict)
        )

The ``MyDataClass.data`` member will now be notified of in place changes
to its value.

Any in-place changes to the ``MyDataClass.data`` member
will flag the attribute as "dirty" on the parent object::

    >>> from sqlalchemy.orm import Session

    >>> sess = Session(some_engine)
    >>> m1 = MyDataClass(data={"value1": "foo"})
    >>> sess.add(m1)
    >>> sess.commit()

    >>> m1.data["value1"] = "bar"
    >>> assert m1 in sess.dirty
    True

The ``MutableDict`` can be associated with all future instances
of ``JSONEncodedDict`` in one step, using
:meth:`~.Mutable.associate_with`.  This is similar to
:meth:`~.Mutable.as_mutable` except it will intercept all occurrences
of ``MutableDict`` in all mappings unconditionally, without
the need to declare it individually::

    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.orm import Mapped
    from sqlalchemy.orm import mapped_column

    MutableDict.associate_with(JSONEncodedDict)


    class Base(DeclarativeBase):
        pass


    class MyDataClass(Base):
        __tablename__ = "my_data"
        id: Mapped[int] = mapped_column(primary_key=True)
        data: Mapped[dict[str, str]] = mapped_column(JSONEncodedDict)

Supporting Pickling
--------------------

The key to the :mod:`sqlalchemy.ext.mutable` extension relies upon the
placement of a ``weakref.WeakKeyDictionary`` upon the value object, which
stores a mapping of parent mapped objects keyed to the attribute name under
which they are associated with this value. ``WeakKeyDictionary`` objects are
not picklable, due to the fact that they contain weakrefs and function
callbacks. In our case, this is a good thing, since if this dictionary were
picklable, it could lead to an excessively large pickle size for our value
objects that are pickled by themselves outside of the context of the parent.
The developer responsibility here is only to provide a ``__getstate__`` method
that excludes the :meth:`~MutableBase._parents` collection from the pickle
stream::

    class MyMutableType(Mutable):
        def __getstate__(self):
            d = self.__dict__.copy()
            d.pop("_parents", None)
            return d

With our dictionary example, we need to return the contents of the dict itself
(and also restore them on __setstate__)::

    class MutableDict(Mutable, dict):
        # ....

        def __getstate__(self):
            return dict(self)

        def __setstate__(self, state):
            self.update(state)

In the case that our mutable value object is pickled as it is attached to one
or more parent objects that are also part of the pickle, the :class:`.Mutable`
mixin will re-establish the :attr:`.Mutable._parents` collection on each value
object as the owning parents themselves are unpickled.

Receiving Events
----------------

The :meth:`.AttributeEvents.modified` event handler may be used to receive
an event when a mutable scalar emits a change event.  This event handler
is called when the :func:`.attributes.flag_modified` function is called
from within the mutable extension::

    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.orm import Mapped
    from sqlalchemy.orm import mapped_column
    from sqlalchemy import event


    class Base(DeclarativeBase):
        pass


    class MyDataClass(Base):
        __tablename__ = "my_data"
        id: Mapped[int] = mapped_column(primary_key=True)
        data: Mapped[dict[str, str]] = mapped_column(
            MutableDict.as_mutable(JSONEncodedDict)
        )


    @event.listens_for(MyDataClass.data, "modified")
    def modified_json(instance, initiator):
        print("json value modified:", instance.data)

Tracking Nested JSON Values
---------------------------

The :class:`.MutableDict` and :class:`.MutableList` types only coerce the
top-level value.  For :class:`.JSON` columns whose root may be an object or
array and whose nested objects and arrays also require change tracking, use
:class:`.MutableJSON`::

    from sqlalchemy import JSON
    from sqlalchemy.ext.mutable import MutableJSON

    class MyDataClass(Base):
        __tablename__ = "my_data"

        id: Mapped[int] = mapped_column(primary_key=True)
        data: Mapped[dict | list] = mapped_column(
            MutableJSON.as_mutable(JSON)
        )

Nested dictionaries and lists are converted to :class:`.MutableJSONDict`
and :class:`.MutableJSONList` on assignment.  The same mutable container may
be referenced from multiple positions or entities; a notification is sent to
every still-associated ORM root.  Replacing or deleting a path decrements the
reference count for that edge only, so other paths continue to notify their
own roots.  Parent links are weak references and do not keep recycled entities
alive.

JSON structures must be acyclic.  Cycles are rejected during coercion before
the target container is modified::

    cyclic = []
    cyclic.append(cyclic)
    MyDataClass(data=cyclic)  # raises ValueError

.. _mutable_composites:

Establishing Mutability on Composites
=====================================

Composites are a special ORM feature which allow a single scalar attribute to
be assigned an object value which represents information "composed" from one
or more columns from the underlying mapped table. The usual example is that of
a geometric "point", and is introduced in :ref:`mapper_composite`.

As is the case with :class:`.Mutable`, the user-defined composite class
subclasses :class:`.MutableComposite` as a mixin, and detects and delivers
change events to its parents via the :meth:`.MutableComposite.changed` method.
In the case of a composite class, the detection is usually via the usage of the
special Python method ``__setattr__()``. In the example below, we expand upon the ``Point``
class introduced in :ref:`mapper_composite` to include
:class:`.MutableComposite` in its bases and to route attribute set events via
``__setattr__`` to the :meth:`.MutableComposite.changed` method::

    import dataclasses
    from sqlalchemy.ext.mutable import MutableComposite


    @dataclasses.dataclass
    class Point(MutableComposite):
        x: int
        y: int

        def __setattr__(self, key, value):
            "Intercept set events"

            # set the attribute
            object.__setattr__(self, key, value)

            # alert all parents to the change
            self.changed()

The :class:`.MutableComposite` class makes use of class mapping events to
automatically establish listeners for any usage of :func:`_orm.composite` that
specifies our ``Point`` type. Below, when ``Point`` is mapped to the ``Vertex``
class, listeners are established which will route change events from ``Point``
objects to each of the ``Vertex.start`` and ``Vertex.end`` attributes::

    from sqlalchemy.orm import DeclarativeBase, Mapped
    from sqlalchemy.orm import composite, mapped_column


    class Base(DeclarativeBase):
        pass


    class Vertex(Base):
        __tablename__ = "vertices"

        id: Mapped[int] = mapped_column(primary_key=True)

        start: Mapped[Point] = composite(
            mapped_column("x1"), mapped_column("y1")
        )
        end: Mapped[Point] = composite(
            mapped_column("x2"), mapped_column("y2")
        )

        def __repr__(self):
            return f"Vertex(start={self.start}, end={self.end})"

Any in-place changes to the ``Vertex.start`` or ``Vertex.end`` members
will flag the attribute as "dirty" on the parent object:

.. sourcecode:: python+sql

    >>> from sqlalchemy.orm import Session
    >>> sess = Session(engine)
    >>> v1 = Vertex(start=Point(3, 4), end=Point(12, 15))
    >>> sess.add(v1)
    {sql}>>> sess.flush()
    BEGIN (implicit)
    INSERT INTO vertices (x1, y1, x2, y2) VALUES (?, ?, ?, ?)
    [...] (3, 4, 12, 15)

    {stop}>>> v1.end.x = 8
    >>> assert v1 in sess.dirty
    True
    {sql}>>> sess.commit()
    UPDATE vertices SET x2=? WHERE vertices.id = ?
    [...] (8, 1)
    COMMIT

Coercing Mutable Composites
---------------------------

The :meth:`.MutableBase.coerce` method is also supported on composite types.
In the case of :class:`.MutableComposite`, the :meth:`.MutableBase.coerce`
method is only called for attribute set operations, not load operations.
Overriding the :meth:`.MutableBase.coerce` method is essentially equivalent
to using a :func:`.validates` validation routine for all attributes which
make use of the custom composite type::

    @dataclasses.dataclass
    class Point(MutableComposite):
        # other Point methods
        # ...

        def coerce(cls, key, value):
            if isinstance(value, tuple):
                value = Point(*value)
            elif not isinstance(value, Point):
                raise ValueError("tuple or Point expected")
            return value

Supporting Pickling
--------------------

As is the case with :class:`.Mutable`, the :class:`.MutableComposite` helper
class uses a ``weakref.WeakKeyDictionary`` available via the
:meth:`MutableBase._parents` attribute which isn't picklable. If we need to
pickle instances of ``Point`` or its owning class ``Vertex``, we at least need
to define a ``__getstate__`` that doesn't include the ``_parents`` dictionary.
Below we define both a ``__getstate__`` and a ``__setstate__`` that package up
the minimal form of our ``Point`` class::

    @dataclasses.dataclass
    class Point(MutableComposite):
        # ...

        def __getstate__(self):
            return self.x, self.y

        def __setstate__(self, state):
            self.x, self.y = state

As with :class:`.Mutable`, the :class:`.MutableComposite` augments the
pickling process of the parent's object-relational state so that the
:meth:`MutableBase._parents` collection is restored to all ``Point`` objects.

"""  # noqa: E501

from __future__ import annotations

from collections import defaultdict
from collections import deque
import copy
from typing import AbstractSet
from typing import Any
from typing import Dict
from typing import Iterable
from typing import List
from typing import NoReturn
from typing import Optional
from typing import overload
from typing import Set
from typing import SupportsIndex
from typing import Tuple
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import Union
import weakref
from weakref import finalize
from weakref import WeakKeyDictionary

from .. import event
from .. import types
from ..orm import Mapper
from ..orm._typing import _ExternalEntityType
from ..orm._typing import _O
from ..orm._typing import _T
from ..orm.attributes import AttributeEventToken
from ..orm.attributes import flag_modified
from ..orm.attributes import InstrumentedAttribute
from ..orm.attributes import QueryableAttribute
from ..orm.context import QueryContext
from ..orm.decl_api import DeclarativeAttributeIntercept
from ..orm.state import InstanceState
from ..orm.unitofwork import UOWTransaction
from ..sql._typing import _TypeEngineArgument
from ..sql.base import SchemaEventTarget
from ..sql.schema import Column
from ..sql.type_api import TypeEngine
from ..util import memoized_property

_KT = TypeVar("_KT")  # Key type.
_VT = TypeVar("_VT")  # Value type.


class MutableBase:
    """Common base class to :class:`.Mutable`
    and :class:`.MutableComposite`.

    """

    @memoized_property
    def _parents(self) -> WeakKeyDictionary[Any, Any]:
        """Dictionary of parent object's :class:`.InstanceState`->attribute
        name on the parent.

        This attribute is a so-called "memoized" property.  It initializes
        itself with a new ``weakref.WeakKeyDictionary`` the first time
        it is accessed, returning the same object upon subsequent access.

        .. versionchanged:: 1.4 the :class:`.InstanceState` is now used
           as the key in the weak dictionary rather than the instance
           itself.

        """

        return weakref.WeakKeyDictionary()

    def _attach_root(self, state: InstanceState[Any], key: str) -> None:
        self._parents[state] = key

    def _detach_root(self, state: InstanceState[Any], key: str) -> None:
        self._parents.pop(state, None)

    @classmethod
    def coerce(cls, key: str, value: Any) -> Optional[Any]:
        """Given a value, coerce it into the target type.

        Can be overridden by custom subclasses to coerce incoming
        data into a particular type.

        By default, raises ``ValueError``.

        This method is called in different scenarios depending on if
        the parent class is of type :class:`.Mutable` or of type
        :class:`.MutableComposite`.  In the case of the former, it is called
        for both attribute-set operations as well as during ORM loading
        operations.  For the latter, it is only called during attribute-set
        operations; the mechanics of the :func:`.composite` construct
        handle coercion during load operations.


        :param key: string name of the ORM-mapped attribute being set.
        :param value: the incoming value.
        :return: the method should return the coerced value, or raise
         ``ValueError`` if the coercion cannot be completed.

        """
        if value is None:
            return None
        msg = "Attribute '%s' does not accept objects of type %s"
        raise ValueError(msg % (key, type(value)))

    @classmethod
    def _get_listen_keys(cls, attribute: QueryableAttribute[Any]) -> Set[str]:
        """Given a descriptor attribute, return a ``set()`` of the attribute
        keys which indicate a change in the state of this attribute.

        This is normally just ``set([attribute.key])``, but can be overridden
        to provide for additional keys.  E.g. a :class:`.MutableComposite`
        augments this set with the attribute keys associated with the columns
        that comprise the composite value.

        This collection is consulted in the case of intercepting the
        :meth:`.InstanceEvents.refresh` and
        :meth:`.InstanceEvents.refresh_flush` events, which pass along a list
        of attribute names that have been refreshed; the list is compared
        against this set to determine if action needs to be taken.

        """
        return {attribute.key}

    @classmethod
    def _listen_on_attribute(
        cls,
        attribute: QueryableAttribute[Any],
        coerce: bool,
        parent_cls: _ExternalEntityType[Any],
    ) -> None:
        """Establish this type as a mutation listener for the given
        mapped descriptor.

        """
        key = attribute.key
        if parent_cls is not attribute.class_:
            return

        # rely on "propagate" here
        parent_cls = attribute.class_

        listen_keys = cls._get_listen_keys(attribute)

        def load(state: InstanceState[_O], *args: Any) -> None:
            """Listen for objects loaded or refreshed.

            Wrap the target data member's value with
            ``Mutable``.

            """
            val = state.dict.get(key, None)
            if val is not None:
                if coerce:
                    val = cls.coerce(key, val)
                    assert val is not None
                    state.dict[key] = val
                val._attach_root(state, key)

        def before_expire(
            state: InstanceState[_O], attrs: Optional[Iterable[Any]]
        ) -> None:
            if attrs is None or listen_keys.intersection(attrs):
                val = state.dict.get(key, None)
                if isinstance(val, MutableBase):
                    val._detach_root(state, key)

        def load_attrs(
            state: InstanceState[_O],
            ctx: Union[object, QueryContext, UOWTransaction],
            attrs: Iterable[Any],
        ) -> None:
            if not attrs or listen_keys.intersection(attrs):
                load(state)

        def set_(
            target: InstanceState[_O],
            value: MutableBase | None,
            oldvalue: MutableBase | None,
            initiator: AttributeEventToken,
        ) -> MutableBase | None:
            """Listen for set/replace events on the target
            data member.

            Establish a weak reference to the parent object
            on the incoming value, remove it for the one
            outgoing.

            """
            if value is oldvalue and value is not None:
                return value

            if not isinstance(value, cls):
                value = cls.coerce(key, value)
            if value is not None:
                value._attach_root(target, key)
            if isinstance(oldvalue, MutableBase):
                oldvalue._detach_root(target, key)
            return value

        def pickle(
            state: InstanceState[_O], state_dict: Dict[str, Any]
        ) -> None:
            val = state.dict.get(key, None)
            if val is not None:
                if "ext.mutable.values" not in state_dict:
                    state_dict["ext.mutable.values"] = defaultdict(list)
                state_dict["ext.mutable.values"][key].append(val)

        def unpickle(
            state: InstanceState[_O], state_dict: Dict[str, Any]
        ) -> None:
            if "ext.mutable.values" in state_dict:
                collection = state_dict["ext.mutable.values"]
                if isinstance(collection, list):
                    # legacy format
                    for val in collection:
                        val._attach_root(state, key)
                else:
                    for val in state_dict["ext.mutable.values"][key]:
                        val._attach_root(state, key)

        event.listen(
            parent_cls,
            "_sa_event_merge_wo_load",
            load,
            raw=True,
            propagate=True,
        )

        event.listen(parent_cls, "load", load, raw=True, propagate=True)
        event.listen(
            parent_cls,
            "before_expire",
            before_expire,
            raw=True,
            propagate=True,
        )
        event.listen(
            parent_cls, "refresh", load_attrs, raw=True, propagate=True
        )
        event.listen(
            parent_cls, "refresh_flush", load_attrs, raw=True, propagate=True
        )
        event.listen(
            attribute, "set", set_, raw=True, retval=True, propagate=True
        )
        event.listen(parent_cls, "pickle", pickle, raw=True, propagate=True)
        event.listen(
            parent_cls, "unpickle", unpickle, raw=True, propagate=True
        )


class Mutable(MutableBase):
    """Mixin that defines transparent propagation of change
    events to a parent object.

    See the example in :ref:`mutable_scalars` for usage information.

    """

    def changed(self) -> None:
        """Subclasses should call this method whenever change events occur."""

        for parent, key in self._parents.items():
            flag_modified(parent.obj(), key)

    @classmethod
    def associate_with_attribute(
        cls, attribute: InstrumentedAttribute[_O]
    ) -> None:
        """Establish this type as a mutation listener for the given
        mapped descriptor.

        """
        cls._listen_on_attribute(attribute, True, attribute.class_)

    @classmethod
    def associate_with(cls, sqltype: type) -> None:
        """Associate this wrapper with all future mapped columns
        of the given type.

        This is a convenience method that calls
        ``associate_with_attribute`` automatically.

        .. warning::

           The listeners established by this method are *global*
           to all mappers, and are *not* garbage collected.   Only use
           :meth:`.associate_with` for types that are permanent to an
           application, not with ad-hoc types else this will cause unbounded
           growth in memory usage.

        """

        def listen_for_type(mapper: Mapper[_O], class_: type) -> None:
            for prop in mapper.column_attrs:
                if isinstance(prop.columns[0].type, sqltype):
                    cls.associate_with_attribute(getattr(class_, prop.key))

        event.listen(Mapper, "mapper_configured", listen_for_type)

    @classmethod
    def as_mutable(cls, sqltype: _TypeEngineArgument[_T]) -> TypeEngine[_T]:
        """Associate a SQL type with this mutable Python type.

        This establishes listeners that will detect ORM mappings against
        the given type, adding mutation event trackers to those mappings.

        The type is returned, unconditionally as an instance, so that
        :meth:`.as_mutable` can be used inline::

            Table(
                "mytable",
                metadata,
                Column("id", Integer, primary_key=True),
                Column("data", MyMutableType.as_mutable(PickleType)),
            )

        Note that the returned type is always an instance, even if a class
        is given, and that only columns which are declared specifically with
        that type instance receive additional instrumentation.

        To associate a particular mutable type with all occurrences of a
        particular type, use the :meth:`.Mutable.associate_with` classmethod
        of the particular :class:`.Mutable` subclass to establish a global
        association.

        .. warning::

           The listeners established by this method are *global*
           to all mappers, and are *not* garbage collected.   Only use
           :meth:`.as_mutable` for types that are permanent to an application,
           not with ad-hoc types else this will cause unbounded growth
           in memory usage.

        """
        sqltype = types.to_instance(sqltype)

        # a SchemaType will be copied when the Column is copied,
        # and we'll lose our ability to link that type back to the original.
        # so track our original type w/ columns
        if isinstance(sqltype, SchemaEventTarget):

            @event.listens_for(sqltype, "before_parent_attach")
            def _add_column_memo(
                sqltyp: TypeEngine[Any],
                parent: Column[_T],
            ) -> None:
                parent.info["_ext_mutable_orig_type"] = sqltyp

            schema_event_check = True
        else:
            schema_event_check = False

        def listen_for_type(
            mapper: Mapper[_T],
            class_: Union[DeclarativeAttributeIntercept, type],
        ) -> None:
            _APPLIED_KEY = "_ext_mutable_listener_applied"

            for prop in mapper.column_attrs:
                if (
                    # all Mutable types refer to a Column that's mapped,
                    # since this is the only kind of Core target the ORM can
                    # "mutate"
                    isinstance(prop.expression, Column)
                    and (
                        (
                            schema_event_check
                            and prop.expression.info.get(
                                "_ext_mutable_orig_type"
                            )
                            is sqltype
                        )
                        or prop.expression.type is sqltype
                    )
                ):
                    if not prop.expression.info.get(_APPLIED_KEY, False):
                        prop.expression.info[_APPLIED_KEY] = True
                        cls.associate_with_attribute(getattr(class_, prop.key))

        event.listen(Mapper, "mapper_configured", listen_for_type)

        return sqltype


class MutableComposite(MutableBase):
    """Mixin that defines transparent propagation of change
    events on a SQLAlchemy "composite" object to its
    owning parent or parents.

    See the example in :ref:`mutable_composites` for usage information.

    """

    @classmethod
    def _get_listen_keys(cls, attribute: QueryableAttribute[_O]) -> Set[str]:
        return {attribute.key}.union(attribute.property._attribute_keys)

    def changed(self) -> None:
        """Subclasses should call this method whenever change events occur."""

        for parent, key in self._parents.items():
            prop = parent.mapper.get_property(key)
            for value, attr_name in zip(
                prop._composite_values_from_instance(self),
                prop._attribute_keys,
            ):
                setattr(parent.obj(), attr_name, value)


def _setup_composite_listener() -> None:
    def _listen_for_type(mapper: Mapper[_T], class_: type) -> None:
        for prop in mapper.iterate_properties:
            if (
                hasattr(prop, "composite_class")
                and isinstance(prop.composite_class, type)
                and issubclass(prop.composite_class, MutableComposite)
            ):
                prop.composite_class._listen_on_attribute(
                    getattr(class_, prop.key), False, class_
                )

    if not event.contains(Mapper, "mapper_configured", _listen_for_type):
        event.listen(Mapper, "mapper_configured", _listen_for_type)


_setup_composite_listener()


class MutableDict(Mutable, Dict[_KT, _VT]):
    """A dictionary type that implements :class:`.Mutable`.

    The :class:`.MutableDict` object implements a dictionary that will
    emit change events to the underlying mapping when the contents of
    the dictionary are altered, including when values are added or removed.

    Note that :class:`.MutableDict` does **not** apply mutable tracking to  the
    *values themselves* inside the dictionary. Therefore it is not a sufficient
    solution for the use case of tracking deep changes to a *recursive*
    dictionary structure, such as a JSON structure.  To support this use case,
    build a subclass of  :class:`.MutableDict` that provides appropriate
    coercion to the values placed in the dictionary so that they too are
    "mutable", and emit events up to their parent structure.

    .. seealso::

        :class:`.MutableList`

        :class:`.MutableSet`

    """

    def __setitem__(self, key: _KT, value: _VT) -> None:
        """Detect dictionary set events and emit change events."""
        dict.__setitem__(self, key, value)
        self.changed()

    if TYPE_CHECKING:
        # from https://github.com/python/mypy/issues/14858

        @overload
        def setdefault(
            self: MutableDict[_KT, Optional[_T]], key: _KT, value: None = None
        ) -> Optional[_T]: ...

        @overload
        def setdefault(self, key: _KT, value: _VT) -> _VT: ...

        def setdefault(self, key: _KT, value: object = None) -> object: ...

    else:

        def setdefault(self, *arg):  # noqa: F811
            result = dict.setdefault(self, *arg)
            self.changed()
            return result

    def __delitem__(self, key: _KT) -> None:
        """Detect dictionary del events and emit change events."""
        dict.__delitem__(self, key)
        self.changed()

    def update(self, *a: Any, **kw: _VT) -> None:
        dict.update(self, *a, **kw)
        self.changed()

    if TYPE_CHECKING:

        @overload
        def pop(self, __key: _KT, /) -> _VT: ...

        @overload
        def pop(self, __key: _KT, default: _VT | _T, /) -> _VT | _T: ...

        def pop(
            self, __key: _KT, __default: _VT | _T | None = None, /
        ) -> _VT | _T: ...

    else:

        def pop(self, *arg):  # noqa: F811
            result = dict.pop(self, *arg)
            self.changed()
            return result

    def popitem(self) -> Tuple[_KT, _VT]:
        result = dict.popitem(self)
        self.changed()
        return result

    def clear(self) -> None:
        dict.clear(self)
        self.changed()

    @classmethod
    def coerce(cls, key: str, value: Any) -> MutableDict[_KT, _VT] | None:
        """Convert plain dictionary to instance of this class."""
        if not isinstance(value, cls):
            if isinstance(value, dict):
                return cls(value)
            return Mutable.coerce(key, value)
        else:
            return value

    def __getstate__(self) -> Dict[_KT, _VT]:
        return dict(self)

    def __setstate__(
        self, state: Union[Dict[str, int], Dict[str, str]]
    ) -> None:
        self.update(state)


class MutableList(Mutable, List[_T]):
    """A list type that implements :class:`.Mutable`.

    The :class:`.MutableList` object implements a list that will
    emit change events to the underlying mapping when the contents of
    the list are altered, including when values are added or removed.

    Note that :class:`.MutableList` does **not** apply mutable tracking to  the
    *values themselves* inside the list. Therefore it is not a sufficient
    solution for the use case of tracking deep changes to a *recursive*
    mutable structure, such as a JSON structure.  To support this use case,
    build a subclass of  :class:`.MutableList` that provides appropriate
    coercion to the values placed in the dictionary so that they too are
    "mutable", and emit events up to their parent structure.

    .. seealso::

        :class:`.MutableDict`

        :class:`.MutableSet`

    """

    def __reduce_ex__(
        self, proto: SupportsIndex
    ) -> Tuple[type, Tuple[List[int]]]:
        return (self.__class__, (list(self),))

    # needed for backwards compatibility with
    # older pickles
    def __setstate__(self, state: Iterable[_T]) -> None:
        self[:] = state

    def __setitem__(
        self, index: SupportsIndex | slice, value: _T | Iterable[_T]
    ) -> None:
        """Detect list set events and emit change events."""
        list.__setitem__(self, index, value)
        self.changed()

    def __delitem__(self, index: SupportsIndex | slice) -> None:
        """Detect list del events and emit change events."""
        list.__delitem__(self, index)
        self.changed()

    def pop(self, *arg: SupportsIndex) -> _T:
        result = list.pop(self, *arg)
        self.changed()
        return result

    def append(self, x: _T) -> None:
        list.append(self, x)
        self.changed()

    def extend(self, x: Iterable[_T]) -> None:
        list.extend(self, x)
        self.changed()

    def __iadd__(self, x: Iterable[_T]) -> MutableList[_T]:  # type: ignore[override,misc] # noqa: E501
        self.extend(x)
        return self

    def insert(self, i: SupportsIndex, x: _T) -> None:
        list.insert(self, i, x)
        self.changed()

    def remove(self, i: _T) -> None:
        list.remove(self, i)
        self.changed()

    def clear(self) -> None:
        list.clear(self)
        self.changed()

    def sort(self, **kw: Any) -> None:
        list.sort(self, **kw)
        self.changed()

    def reverse(self) -> None:
        list.reverse(self)
        self.changed()

    @classmethod
    def coerce(
        cls, key: str, value: MutableList[_T] | _T
    ) -> Optional[MutableList[_T]]:
        """Convert plain list to instance of this class."""
        if not isinstance(value, cls):
            if isinstance(value, list):
                return cls(value)
            return Mutable.coerce(key, value)
        else:
            return value


class MutableSet(Mutable, Set[_T]):
    """A set type that implements :class:`.Mutable`.

    The :class:`.MutableSet` object implements a set that will
    emit change events to the underlying mapping when the contents of
    the set are altered, including when values are added or removed.

    Note that :class:`.MutableSet` does **not** apply mutable tracking to  the
    *values themselves* inside the set. Therefore it is not a sufficient
    solution for the use case of tracking deep changes to a *recursive*
    mutable structure.  To support this use case,
    build a subclass of  :class:`.MutableSet` that provides appropriate
    coercion to the values placed in the dictionary so that they too are
    "mutable", and emit events up to their parent structure.

    .. seealso::

        :class:`.MutableDict`

        :class:`.MutableList`


    """

    def update(self, *arg: Iterable[_T]) -> None:
        set.update(self, *arg)
        self.changed()

    def intersection_update(self, *arg: Iterable[Any]) -> None:
        set.intersection_update(self, *arg)
        self.changed()

    def difference_update(self, *arg: Iterable[Any]) -> None:
        set.difference_update(self, *arg)
        self.changed()

    def symmetric_difference_update(self, *arg: Iterable[_T]) -> None:
        set.symmetric_difference_update(self, *arg)
        self.changed()

    def __ior__(self, other: AbstractSet[_T]) -> MutableSet[_T]:  # type: ignore[override,misc] # noqa: E501
        self.update(other)
        return self

    def __iand__(self, other: AbstractSet[object]) -> MutableSet[_T]:
        self.intersection_update(other)
        return self

    def __ixor__(self, other: AbstractSet[_T]) -> MutableSet[_T]:  # type: ignore[override,misc] # noqa: E501
        self.symmetric_difference_update(other)
        return self

    def __isub__(self, other: AbstractSet[object]) -> MutableSet[_T]:  # type: ignore[misc,unused-ignore] # noqa: E501
        self.difference_update(other)
        return self

    def add(self, elem: _T) -> None:
        set.add(self, elem)
        self.changed()

    def remove(self, elem: _T) -> None:
        set.remove(self, elem)
        self.changed()

    def discard(self, elem: _T) -> None:  # type: ignore[override,unused-ignore] # noqa: E501
        set.discard(self, elem)
        self.changed()

    def pop(self, *arg: Any) -> _T:
        result = set.pop(self, *arg)
        self.changed()
        return result

    def clear(self) -> None:
        set.clear(self)
        self.changed()

    @classmethod
    def coerce(cls, index: str, value: Any) -> Optional[MutableSet[_T]]:
        """Convert plain set to instance of this class."""
        if not isinstance(value, cls):
            if isinstance(value, set):
                return cls(value)
            return Mutable.coerce(index, value)
        else:
            return value

    def __getstate__(self) -> Set[_T]:
        return set(self)

    def __setstate__(self, state: Iterable[_T]) -> None:
        self.update(state)

    def __reduce_ex__(
        self, proto: SupportsIndex
    ) -> Tuple[type, Tuple[List[int]]]:
        return (self.__class__, (list(self),))


class _MutableJSONValue(Mutable):
    @memoized_property
    def _json_edges(
        self,
    ) -> Dict[int, Tuple[Any, int]]:
        return {}

    @memoized_property
    def _json_roots(self) -> WeakKeyDictionary[InstanceState[Any], Set[str]]:
        return weakref.WeakKeyDictionary()

    def _iter_json_children(self) -> Iterable[_MutableJSONValue]:
        raise NotImplementedError

    def _attach_root(self, state: InstanceState[Any], key: str) -> None:
        roots = self._json_roots.setdefault(state, set())
        if not roots:
            seen: Set[int] = set()
            queued: Set[int] = {id(self)}
            pending = deque([self])
            while pending:
                current = pending.popleft()
                child_edges = {}
                for child in current._iter_json_children():
                    child_id = id(child)
                    if child_id not in child_edges:
                        child_edges[child_id] = [
                            child,
                            child._json_edges.get(id(current)),
                            0,
                        ]
                    child_edges[child_id][2] += 1
                for child, edge, multiplicity in child_edges.values():
                    edges = child._json_edges
                    if edge is None:
                        edges[id(current)] = (
                            finalize(
                                current,
                                child._json_edges.pop,
                                id(current),
                                None,
                            ),
                            multiplicity,
                        )
                    else:
                        edges[id(current)] = (
                            edge[0],
                            edge[1] + multiplicity,
                        )
                for child, _, _ in child_edges.values():
                    child_id = id(child)
                    if child_id not in seen and child_id not in queued:
                        queued.add(child_id)
                        pending.append(child)
                seen.add(id(current))
                queued.discard(id(current))
        self._parents[state] = key
        roots.add(key)

    def _detach_root(self, state: InstanceState[Any], key: str) -> None:
        roots = self._json_roots.get(state)
        if roots is None:
            return
        roots.discard(key)
        if roots:
            self._parents[state] = next(iter(roots))
            return
        self._parents.pop(state, None)
        del self._json_roots[state]

        seen: Set[int] = set()
        queued: Set[int] = {id(self)}
        pending = deque([self])
        while pending:
            current = pending.popleft()
            child_edges = {}
            for child in current._iter_json_children():
                child_id = id(child)
                if child_id not in child_edges:
                    child_edges[child_id] = [
                        child,
                        child._json_edges.get(id(current)),
                        0,
                    ]
                child_edges[child_id][2] += 1
            for child, edge, multiplicity in child_edges.values():
                if edge is None:
                    continue
                count = edge[1] - multiplicity
                if count:
                    child._json_edges[id(current)] = (edge[0], count)
                else:
                    edge[0].detach()
                    del child._json_edges[id(current)]
            for child, _, _ in child_edges.values():
                child_id = id(child)
                if child_id not in seen and child_id not in queued:
                    queued.add(child_id)
                    pending.append(child)
            seen.add(id(current))
            queued.discard(id(current))

    def _attach_container(self, parent: "_MutableJSONValue") -> None:
        edges = self._json_edges
        edge = edges.get(id(parent))
        if edge is None:
            edges[id(parent)] = (
                finalize(
                    parent,
                    self._json_edges.pop,
                    id(parent),
                    None,
                ),
                1,
            )
        else:
            edges[id(parent)] = (edge[0], edge[1] + 1)

    def _detach_container(self, parent: "_MutableJSONValue") -> None:
        edge = self._json_edges.get(id(parent))
        if edge is None:
            return
        count = edge[1] - 1
        if count:
            self._json_edges[id(parent)] = (edge[0], count)
        else:
            edge[0].detach()
            del self._json_edges[id(parent)]

    def _rejects_json_cycle(self, value: Any) -> bool:
        if not isinstance(value, (dict, list, tuple)):
            return False
        seen: Set[int] = set()
        active: Set[int] = set()
        pending = [(value, False)]
        while pending:
            current, exiting = pending.pop()
            current_id = id(current)
            if exiting:
                active.discard(current_id)
                seen.add(current_id)
                continue
            if current_id in active:
                return True
            if current_id in seen:
                continue
            active.add(current_id)
            if current is self:
                return True
            pending.append((current, True))
            if isinstance(current, _MutableJSONValue):
                children = current._iter_json_children()
            elif isinstance(current, dict):
                children = current.values()
            else:
                children = current
            for child in children:
                if isinstance(child, (dict, list, tuple)):
                    pending.append((child, False))
        return False

    def _coerce_json_child(self, value: Any) -> Any:
        if isinstance(value, (dict, list, tuple)):
            if self._rejects_json_cycle(value):
                raise ValueError(
                    "JSON values cannot contain cyclic references"
                )
            return _coerce_mutable_json_value("JSON", value, {})
        return value

    def _coerce_or_keep_json_child(self, value: Any) -> Any:
        if isinstance(value, _MutableJSONValue):
            if self._rejects_json_cycle(value):
                raise ValueError(
                    "JSON values cannot contain cyclic references"
                )
            return value
        return self._coerce_json_child(value)

    def _attach_json_child(self, value: Any) -> None:
        if isinstance(value, _MutableJSONValue):
            value._attach_container(self)

    def _detach_json_child(self, value: Any) -> None:
        if isinstance(value, _MutableJSONValue):
            value._detach_container(self)

    def changed(self) -> None:
        seen: Set[int] = set()
        pending: deque[_MutableJSONValue] = deque([self])
        while pending:
            current = pending.popleft()
            if id(current) in seen:
                continue
            seen.add(id(current))

            active = False
            for parent, key in current._parents.items():
                obj = parent.obj()
                if obj is not None:
                    flag_modified(obj, key)
                    active = True

            for edge_finalize, _ in current._json_edges.values():
                parent = edge_finalize.peek()[0]
                if parent is not None:
                    pending.append(parent)
                    active = True

    @staticmethod
    def _coerce_value(key: str, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            _validate_mutable_json_value(key, value, set())
        return _coerce_mutable_json_value(key, value, {})


def _validate_mutable_json_value(
    key: str,
    value: Any,
    seen: Set[int],
) -> None:
    if (
        not isinstance(value, (dict, list, tuple))
        or id(value) in seen
    ):
        return

    active: Set[int] = set()
    pending = [(value, False)]
    while pending:
        current, exiting = pending.pop()
        current_id = id(current)
        if exiting:
            active.discard(current_id)
            seen.add(current_id)
            continue
        if current_id in seen:
            continue
        if current_id in active:
            raise ValueError(
                "Attribute '%s' does not accept cyclic JSON values" % key
            )
        active.add(current_id)
        pending.append((current, True))
        children = current.values() if isinstance(current, dict) else current
        child_containers = [
            child
            for child in children
            if isinstance(child, (dict, list, tuple))
        ]
        for child in child_containers:
            if id(child) in active:
                raise ValueError(
                    "Attribute '%s' does not accept cyclic JSON values" % key
                )
            if id(child) not in seen:
                pending.append((child, False))


def _json_coercion_cache() -> Dict[int, _MutableJSONValue]:
    if len(_JSON_COERCION_CACHE) > 4096:
        _JSON_COERCION_CACHE.clear()
    return _JSON_COERCION_CACHE


_JSON_COERCION_CACHE: Dict[int, _MutableJSONValue] = {}


def _coerce_mutable_json_value(
    key: str,
    value: Any,
    converted: Dict[int, _MutableJSONValue],
) -> Any:
    cached = _json_coercion_cache().get(id(value))
    if cached is not None:
        return cached
    if isinstance(value, _MutableJSONValue):
        existing = converted.get(id(value))
        if existing is not None:
            return existing
        converted[id(value)] = value
        if isinstance(value, MutableJSONDict):
            for child_key, child_value in tuple(dict.items(value)):
                dict.__setitem__(
                    value,
                    child_key,
                    _coerce_mutable_json_value(key, child_value, converted),
                )
        else:
            for index, child_value in enumerate(list(value)):
                list.__setitem__(
                    value,
                    index,
                    _coerce_mutable_json_value(key, child_value, converted),
                )
        return value

    existing = converted.get(id(value))
    if existing is not None:
        return existing

    if isinstance(value, dict):
        result: _MutableJSONValue = MutableJSONDict()
        converted[id(value)] = result
        _json_coercion_cache()[id(value)] = result
        for child_key, child_value in value.items():
            dict.__setitem__(
                result,
                child_key,
                _coerce_mutable_json_value(key, child_value, converted),
            )
        return result

    if isinstance(value, list):
        result = MutableJSONList()
        converted[id(value)] = result
        _json_coercion_cache()[id(value)] = result
        for child_value in value:
            list.append(
                result,
                _coerce_mutable_json_value(key, child_value, converted),
            )
        return result

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    return Mutable.coerce(key, value)


class MutableJSONDict(_MutableJSONValue, MutableDict):
    """A JSON object that tracks nested object and array mutations."""

    def __deepcopy__(self, memo: Dict[int, Any]) -> "MutableJSONDict":
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        result = self.__class__()
        memo[id(self)] = result
        for key, value in dict.items(self):
            dict.__setitem__(
                result,
                copy.deepcopy(key, memo),
                self._coerce_json_child(copy.deepcopy(value, memo)),
            )
        return result

    def _iter_json_children(self) -> Iterable[_MutableJSONValue]:
        for value in dict.values(self):
            if isinstance(value, _MutableJSONValue):
                yield value

    def __setitem__(self, key: Any, value: Any) -> None:
        old = dict.get(self, key)
        value = self._coerce_or_keep_json_child(value)
        self._attach_json_child(value)
        dict.__setitem__(self, key, value)
        self._detach_json_child(old)
        self.changed()

    def __delitem__(self, key: Any) -> None:
        old = dict.__getitem__(self, key)
        dict.__delitem__(self, key)
        self._detach_json_child(old)
        self.changed()

    def setdefault(self, *arg: Any) -> Any:
        key = arg[0]
        if key in self:
            return dict.get(self, key)
        value = self._coerce_or_keep_json_child(
            arg[1] if len(arg) > 1 else None
        )
        self._attach_json_child(value)
        dict.__setitem__(self, key, value)
        self.changed()
        return value

    def update(self, *arg: Any, **kw: Any) -> None:
        if arg:
            if len(arg) > 1:
                raise TypeError(
                    "update expected at most 1 argument, got %d" % len(arg)
                )
            source = arg[0]
            if hasattr(source, "keys"):
                items = [
                    (child_key, source[child_key])
                    for child_key in source.keys()
                ]
            else:
                items = list(source)
        else:
            items = []
        items.extend(kw.items())
        coerced = [
            (child_key, self._coerce_or_keep_json_child(child_value))
            for child_key, child_value in items
        ]
        old_values = [dict.get(self, child_key) for child_key, _ in coerced]
        for _, child_value in coerced:
            self._attach_json_child(child_value)
        new_value = {key: value for key, value in coerced}
        dict.update(self, new_value)
        for old in old_values:
            self._detach_json_child(old)
        self.changed()

    def pop(self, *arg: Any) -> Any:
        key = arg[0]
        if key in self:
            old = dict.__getitem__(self, key)
            result = dict.pop(self, key)
            self._detach_json_child(old)
            self.changed()
            return result
        if len(arg) > 1:
            return arg[1]
        raise KeyError(key)

    def popitem(self) -> Tuple[Any, Any]:
        key, old = dict.popitem(self)
        self._detach_json_child(old)
        self.changed()
        return key, old

    def clear(self) -> None:
        old_values = list(dict.values(self))
        dict.clear(self)
        for old in old_values:
            self._detach_json_child(old)
        self.changed()

    @classmethod
    def coerce(cls, key: str, value: Any) -> Optional["MutableJSONDict"]:
        if value is None:
            return None
        result = _MutableJSONValue._coerce_value(key, value)
        if isinstance(result, MutableJSONDict):
            return result
        return Mutable.coerce(key, value)


class MutableJSONList(_MutableJSONValue, MutableList):
    """A JSON array that tracks nested object and array mutations."""

    def __deepcopy__(self, memo: Dict[int, Any]) -> "MutableJSONList":
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        result = self.__class__()
        memo[id(self)] = result
        for value in list.__iter__(self):
            list.append(
                result,
                self._coerce_json_child(copy.deepcopy(value, memo)),
            )
        return result

    def _iter_json_children(self) -> Iterable[_MutableJSONValue]:
        for value in list.__iter__(self):
            if isinstance(value, _MutableJSONValue):
                yield value

    def __setitem__(self, index: Any, value: Any) -> None:
        if isinstance(index, slice):
            old_values = list.__getitem__(self, index)
            coerced = [
                self._coerce_or_keep_json_child(item)
                for item in value
            ]
            for item in coerced:
                self._attach_json_child(item)
            list.__setitem__(self, index, coerced)
            for old in old_values:
                self._detach_json_child(old)
        else:
            old = list.__getitem__(self, index)
            value = self._coerce_or_keep_json_child(value)
            self._attach_json_child(value)
            list.__setitem__(self, index, value)
            self._detach_json_child(old)
        self.changed()

    def __delitem__(self, index: Any) -> None:
        old_values = (
            list.__getitem__(self, index)
            if isinstance(index, slice)
            else [list.__getitem__(self, index)]
        )
        list.__delitem__(self, index)
        for old in old_values:
            self._detach_json_child(old)
        self.changed()

    def pop(self, *arg: Any) -> Any:
        result = list.pop(self, *arg)
        self._detach_json_child(result)
        self.changed()
        return result

    def append(self, value: Any) -> None:
        value = self._coerce_or_keep_json_child(value)
        self._attach_json_child(value)
        list.append(self, value)
        self.changed()

    def extend(self, values: Iterable[Any]) -> None:
        coerced = [
            self._coerce_or_keep_json_child(value)
            for value in values
        ]
        for value in coerced:
            self._attach_json_child(value)
        list.extend(self, coerced)
        self.changed()

    def insert(self, index: SupportsIndex, value: Any) -> None:
        value = self._coerce_or_keep_json_child(value)
        self._attach_json_child(value)
        list.insert(self, index, value)
        self.changed()

    def remove(self, value: Any) -> None:
        index = list.index(self, value)
        old = list.__getitem__(self, index)
        list.remove(self, value)
        self._detach_json_child(old)
        self.changed()

    def clear(self) -> None:
        old_values = list(self)
        list.clear(self)
        for old in old_values:
            self._detach_json_child(old)
        self.changed()

    @classmethod
    def coerce(cls, key: str, value: Any) -> Optional["MutableJSONList"]:
        if value is None:
            return None
        result = _MutableJSONValue._coerce_value(key, value)
        if isinstance(result, MutableJSONList):
            return result
        return Mutable.coerce(key, value)


class MutableJSON(_MutableJSONValue):
    """Mutable root for a JSON object or array.

    Use with :meth:`.Mutable.as_mutable` when the column root may be either a
    dict or list.  Plain nested containers are coerced recursively while
    cyclic structures are rejected.
    """

    def __new__(cls) -> NoReturn:  # type: ignore[override]
        if cls is not MutableJSON:
            return super().__new__(cls)
        raise ValueError(
            "MutableJSON root must be a dictionary or a list; use "
            "MutableJSONDict or MutableJSONList to instantiate a container"
        )

    @classmethod
    def coerce(cls, key: str, value: Any) -> Optional[_MutableJSONValue]:
        if value is None:
            return None
        result = _MutableJSONValue._coerce_value(key, value)
        if isinstance(result, _MutableJSONValue):
            return result
        return Mutable.coerce(key, value)
