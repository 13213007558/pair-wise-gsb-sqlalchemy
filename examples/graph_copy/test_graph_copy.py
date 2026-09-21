"""Unit tests and runnable SQLite integration example for
``graph_copy.py``.

Run directly with pytest::

    $ python -m pytest examples/graph_copy/test_graph_copy.py

"""

import contextlib
from datetime import datetime
from typing import Optional

import pytest

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import ForeignKey
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy.orm import attribute_keyed_dict
from sqlalchemy.orm import attributes
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import object_session
from sqlalchemy.orm import relationship
from sqlalchemy.orm import selectinload
from sqlalchemy.orm import Session

from .graph_copy import copy_graph
from .graph_copy import GraphCopier
from .graph_copy import UnloadedAttributeError


class Base(DeclarativeBase):
    pass


doc_tags = Table(
    "doc_tags",
    Base.metadata,
    Column("doc_id", ForeignKey("documents.id"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(50))

    documents: Mapped[set["Document"]] = relationship(
        secondary=doc_tags, back_populates="tags"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    version_id: Mapped[int] = mapped_column(nullable=False)

    tags: Mapped[set[Tag]] = relationship(
        secondary=doc_tags, back_populates="documents"
    )
    lines: Mapped[list["DocLine"]] = relationship(
        back_populates="document", order_by="DocLine.line_no"
    )

    __mapper_args__ = {"version_id_col": version_id}


class DocLine(Base):
    """Composite primary key: (document_id, line_no)."""

    __tablename__ = "doc_lines"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id"), primary_key=True
    )
    line_no: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(String(100))

    document: Mapped[Document] = relationship(back_populates="lines")


class Employee(Base):
    """Joined-table inheritance base with a self-referential mentor
    relationship (used to build cycles)."""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20))
    mentor_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("employees.id")
    )

    # post_update breaks the INSERT dependency cycle for mutual
    # mentorship loops (the FK is written with a follow-up UPDATE)
    mentor: Mapped[Optional["Employee"]] = relationship(
        remote_side=[id], back_populates="proteges", post_update=True
    )
    proteges: Mapped[dict[str, "Employee"]] = relationship(
        collection_class=attribute_keyed_dict("name"),
        back_populates="mentor",
        post_update=True,
    )

    __mapper_args__ = {
        "polymorphic_on": "kind",
        "polymorphic_identity": "employee",
    }


class Engineer(Employee):
    __tablename__ = "engineers"

    id: Mapped[int] = mapped_column(
        ForeignKey("employees.id"), primary_key=True
    )
    language: Mapped[str] = mapped_column(String(30))

    __mapper_args__ = {"polymorphic_identity": "engineer"}


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as sess:
        yield sess


@contextlib.contextmanager
def count_statements(engine):
    """Yield a callable returning the number of SQL statements executed
    while the context was active."""
    count = 0

    def before_cursor_execute(*args):
        nonlocal count
        count += 1

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield lambda: count
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)


def make_documents(session):
    shared = Tag(label="shared")
    solo = Tag(label="solo")
    d1 = Document(
        title="d1",
        tags={shared, solo},
        lines=[DocLine(line_no=1, text="l1"), DocLine(line_no=2, text="l2")],
    )
    d2 = Document(
        title="d2", tags={shared}, lines=[DocLine(line_no=1, text="x")]
    )
    session.add_all([d1, d2])
    session.commit()
    # server_default columns are expired by the flush; a caller must
    # load them explicitly before copying
    d1.created_at
    d2.created_at
    return d1, d2, shared, solo


def load_documents(session):
    """Explicitly load the whole document graph (e.g. after
    ``expire_all()``) so that copying emits no SQL."""
    session.scalars(
        select(Document).options(
            selectinload(Document.lines), selectinload(Document.tags)
        )
    ).unique().all()
    session.scalars(select(Tag).options(selectinload(Tag.documents))).all()
    # eager loading populates collections, but the many-to-one back
    # references stay unloaded; touch them so the copy emits no SQL
    for doc in session.scalars(select(Document)).unique().all():
        for line in doc.lines:
            line.document
        for tag in doc.tags:
            tag.documents


def make_employees(session):
    e1 = Employee(name="ann")
    e2 = Engineer(name="bob", language="python")
    # self-referential cycle: ann <-> bob mentor each other
    e1.mentor = e2
    e2.mentor = e1
    session.add_all([e1, e2])
    session.commit()
    return e1, e2


def load_employees(session):
    employees = session.scalars(
        select(Employee).options(
            selectinload(Employee.mentor), selectinload(Employee.proteges)
        )
    ).all()
    for employee in employees:
        if isinstance(employee, Engineer):
            employee.language  # joined-table row, loaded explicitly


def test_shared_node_m2m_and_composite_pk(engine, session):
    d1, d2, shared, solo = make_documents(session)
    session.expire_all()
    load_documents(session)

    with count_statements(engine) as count:
        c1, c2 = GraphCopier().copy([d1, d2])
    assert count() == 0

    # the tag shared by both documents is copied exactly once
    shared_c1 = {t.label: t for t in c1.tags}["shared"]
    shared_c2 = {t.label: t for t in c2.tags}["shared"]
    assert shared_c1 is shared_c2
    assert shared_c1 is not shared
    assert {t.label for t in c1.tags} == {"shared", "solo"}

    # composite PK child: line_no copied, FK left for the flush
    line = c1.lines[0]
    assert line.line_no == 1
    assert line.document_id is None
    assert line.document is c1

    # copies are transient and share no ORM-internal state
    assert inspect(c1).transient
    assert inspect(shared_c1).transient
    assert (
        c1.__dict__["_sa_instance_state"]
        is not d1.__dict__["_sa_instance_state"]
    )

    # persist the new graph in a *new* session
    session2 = Session(engine)
    session2.add(c1)
    session2.add(c2)
    session2.commit()

    assert c1.id is not None and c1.id != d1.id
    assert line.document_id == c1.id
    assert shared_c1.id is not None and shared_c1.id != shared.id

    # m2m rows exist for the new graph, original rows are intact
    pairs = set(
        session2.execute(select(doc_tags.c.doc_id, doc_tags.c.tag_id))
    )
    assert (c1.id, shared_c1.id) in pairs
    assert (d1.id, shared.id) in pairs
    assert session2.scalar(select(func.count()).select_from(Tag)) == 4
    assert session2.scalar(select(func.count()).select_from(DocLine)) == 6


def test_cycle_self_reference_and_joined_inheritance(engine, session):
    e1, e2 = make_employees(session)
    session.expire_all()
    load_employees(session)

    with count_statements(engine) as count:
        c1 = GraphCopier().copy(e1)
    assert count() == 0

    assert c1 is not e1
    assert type(c1) is Employee
    c2 = c1.mentor
    assert type(c2) is Engineer
    assert c2 is not e2
    assert c2.language == "python"
    assert c2.kind == "engineer"

    # the cycle is preserved topologically, without infinite recursion
    assert c2.mentor is c1
    # dict-keyed collection: keys preserved, values are the copies
    assert c1.proteges == {"bob": c2}
    assert c2.proteges == {"ann": c1}

    # FK columns are not copied; they are synchronized on flush
    assert c1.mentor_id is None

    session2 = Session(engine)
    session2.add(c1)
    session2.commit()

    assert c1.id is not None and c1.id != e1.id
    assert c2.id is not None and c2.id != e2.id
    assert c1.mentor_id == c2.id
    assert c2.mentor_id == c1.id
    assert session2.scalar(select(func.count()).select_from(Employee)) == 4
    assert session2.scalar(select(func.count()).select_from(Engineer)) == 2

    # source graph untouched
    assert e1.mentor is e2
    assert object_session(e1) is session


def test_generated_values_not_copied(engine, session):
    d1, d2, shared, solo = make_documents(session)
    d1.title = "d1 v2"
    session.commit()
    d1.title = "d1 v3"
    session.commit()
    assert d1.version_id == 3

    session.expire_all()
    load_documents(session)
    c1 = GraphCopier().copy(d1)

    # autoincrement PK, version column and server_default column are
    # all left for the database / ORM to generate
    assert c1.id is None
    assert c1.version_id is None
    assert c1.created_at is None

    session2 = Session(engine)
    session2.add(c1)
    session2.commit()

    assert c1.id is not None and c1.id != d1.id
    assert c1.version_id == 1
    assert isinstance(c1.created_at, datetime)
    assert c1.title == "d1 v3"

    # source row untouched
    assert d1.version_id == 3
    assert object_session(d1) is session


def test_unloaded_attributes_fail_without_sql(engine, session):
    d1, d2, shared, solo = make_documents(session)
    session.expire_all()

    with count_statements(engine) as count:
        with pytest.raises(UnloadedAttributeError) as excinfo:
            GraphCopier().copy(d1)
    assert count() == 0

    # every missing path is reported so the caller can load it
    missing = excinfo.value.missing
    assert "Document.title" in missing
    assert "Document.created_at" in missing
    assert "Document.tags" in missing
    assert "Document.lines" in missing

    # after explicit loading, the copy succeeds and still emits no SQL
    load_documents(session)
    with count_statements(engine) as count:
        c1 = GraphCopier().copy(d1)
    assert count() == 0
    assert c1.title == "d1"


def test_allow_unloaded_opt_in_emits_sql(engine, session):
    d1, d2, shared, solo = make_documents(session)
    session.expire_all()

    with count_statements(engine) as count:
        c1 = GraphCopier(allow_unloaded=True).copy(d1)
    assert count() > 0
    assert c1.title == "d1"


def test_excluded_relationship_ignore(engine, session):
    d1, d2, shared, solo = make_documents(session)

    c1 = GraphCopier(exclude=[(Document, "tags")]).copy(d1)

    assert c1.tags == set()
    assert [line.text for line in c1.lines] == ["l1", "l2"]


def test_excluded_relationship_keep_same_session(engine, session):
    d1, d2, shared, solo = make_documents(session)

    c1 = GraphCopier(
        exclude=[(Document, "tags")], on_excluded="keep"
    ).copy(d1)

    # the copy references the *original* tags, in a fresh container
    assert c1.tags is not d1.tags
    assert c1.tags == d1.tags
    assert shared in c1.tags

    # "keep" requires the kept objects to be persistent in the Session
    # where the copies are flushed - here, the same Session
    session.add(c1)
    session.commit()

    assert c1.id is not None
    # no new tag rows were produced
    assert session.scalar(select(func.count()).select_from(Tag)) == 2
    pairs = set(
        session.execute(select(doc_tags.c.doc_id, doc_tags.c.tag_id))
    )
    assert (c1.id, shared.id) in pairs


def test_source_state_and_history_unchanged(engine, session):
    d1, d2, shared, solo = make_documents(session)
    assert not (session.dirty or session.new or session.deleted)

    history_before = attributes.get_history(d1, "title")
    tags_before = set(d1.tags)
    lines_before = list(d1.lines)
    identity_before = inspect(d1).identity

    GraphCopier().copy(d1)

    assert not (session.dirty or session.new or session.deleted)
    assert attributes.get_history(d1, "title") == history_before
    assert set(d1.tags) == tags_before
    assert d1.lines == lines_before
    assert inspect(d1).identity == identity_before
    assert object_session(d1) is session


def test_copy_graph_convenience(engine, session):
    d1, d2, shared, solo = make_documents(session)

    c1 = copy_graph(d1)

    assert inspect(c1).transient
    assert c1.title == "d1"
    assert c1 is not d1
