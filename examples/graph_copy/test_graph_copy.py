"""SQLite integration tests / runnable example for the graph_copy module.

Run with:  python -m pytest examples/graph_copy/test_graph_copy.py
"""

import pytest

from sqlalchemy import Column
from sqlalchemy import create_engine
from sqlalchemy import DateTime
from sqlalchemy import event
from sqlalchemy import ForeignKey
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import Integer
from sqlalchemy import PrimaryKeyConstraint
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import Text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import deferred
from sqlalchemy.orm import relationship
from sqlalchemy.orm import Session

from .graph_copy import copy_graph
from .graph_copy import IGNORE
from .graph_copy import KEEP
from .graph_copy import UnloadedAttributeError

Base = declarative_base()


class User(Base):
    __tablename__ = "user"
    id = Column(Integer, primary_key=True)
    name = Column(String(50))


class Release(Base):
    __tablename__ = "release"
    id = Column(Integer, primary_key=True)
    name = Column(String(50))
    version_id = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, server_default=func.now())
    notes = deferred(Column(Text))
    sections = relationship(
        "Section", back_populates="release", order_by="Section.id"
    )
    items = relationship("Item", back_populates="release")

    __mapper_args__ = {"version_id_col": version_id}


class Section(Base):
    __tablename__ = "section"
    id = Column(Integer, primary_key=True)
    release_id = Column(ForeignKey("release.id"))
    title = Column(String(50))
    kind = Column(String(20))
    release = relationship("Release", back_populates="sections")
    items = relationship("Item", back_populates="section")

    __mapper_args__ = {
        "polymorphic_on": kind,
        "polymorphic_identity": "plain",
    }


class AdvancedSection(Section):
    __tablename__ = "advanced_section"
    id = Column(ForeignKey("section.id"), primary_key=True)
    extra = Column(String(50))

    __mapper_args__ = {"polymorphic_identity": "advanced"}


item_tag = Table(
    "item_tag",
    Base.metadata,
    Column("item_release_id", Integer),
    Column("item_code", String(20)),
    Column("tag_id", ForeignKey("tag.id")),
    ForeignKeyConstraint(
        ["item_release_id", "item_code"],
        ["item.release_id", "item.code"],
    ),
    PrimaryKeyConstraint("item_release_id", "item_code", "tag_id"),
)


class Item(Base):
    """Composite natural PK (release_id, code); release_id is also an FK."""

    __tablename__ = "item"
    release_id = Column(ForeignKey("release.id"), primary_key=True)
    code = Column(String(20), primary_key=True)
    label = Column(String(50))
    section_id = Column(ForeignKey("section.id"))
    owner_id = Column(ForeignKey("user.id"))
    release = relationship("Release", back_populates="items")
    section = relationship("Section", back_populates="items")
    owner = relationship("User")
    tags = relationship("Tag", secondary=item_tag, back_populates="items")


class Tag(Base):
    __tablename__ = "tag"
    id = Column(Integer, primary_key=True)
    name = Column(String(50))
    items = relationship("Item", secondary=item_tag, back_populates="tags")


class Node(Base):
    """Self-referential adjacency; post_update allows cycles on flush."""

    __tablename__ = "node"
    id = Column(Integer, primary_key=True)
    name = Column(String(20))
    parent_id = Column(Integer, ForeignKey("node.id"))
    parent = relationship(
        "Node", remote_side=[id], backref="children", post_update=True
    )


class SQLCounter:
    """Counts statements emitted against an engine."""

    def __init__(self, engine):
        self.engine = engine
        self.count = 0

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._inc)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._inc)

    def _inc(self, *args):
        self.count += 1


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


def build_release_graph(session):
    user = User(name="alice")
    release = Release(name="v1-config", notes="secret notes")
    plain = Section(title="general", release=release)
    advanced = AdvancedSection(
        title="advanced", extra="adv-data", release=release
    )
    tag_a = Tag(name="tag-a")
    shared = Tag(name="shared")
    Item(
        code="A",
        label="item-a",
        release=release,
        section=plain,
        owner=user,
        tags=[tag_a, shared],
    )
    Item(
        code="B",
        label="item-b",
        release=release,
        section=advanced,
        owner=user,
        tags=[shared],
    )
    session.add(release)
    session.commit()
    return release


def fully_load(release):
    """Touch every attribute the copy needs (explicit loading)."""
    release.name, release.notes
    for section in release.sections:
        section.title, section.release
        if isinstance(section, AdvancedSection):
            section.extra
        for item in section.items:
            item.label, item.code, item.release, item.section
            item.owner.name
            for tag in item.tags:
                tag.name, tag.items
    for item in release.items:
        item.label


@pytest.fixture()
def session(engine):
    sess = Session(engine, expire_on_commit=False)
    release = build_release_graph(sess)
    fully_load(release)
    yield sess, release
    sess.close()


def copied_items(copied_release):
    return {item.code: item for item in copied_release.items}


def test_shared_node_copied_once(session):
    sess, release = session
    copied = copy_graph(release)

    items = copied_items(copied)
    shared_from_a = [t for t in items["A"].tags if t.name == "shared"]
    shared_from_b = items["B"].tags
    assert len(shared_from_a) == 1
    assert len(shared_from_b) == 1
    # one single copy for the tag reachable through two paths
    assert shared_from_a[0] is shared_from_b[0]
    original_shared = sess.query(Tag).filter_by(name="shared").one()
    assert shared_from_a[0] is not original_shared

    # the same root passed twice is also memoized
    first, second = copy_graph([release, release])
    assert first is second


def test_cycle_preserved(session):
    sess, _ = session
    n1, n2 = Node(name="n1"), Node(name="n2")
    n1.parent = n2
    n2.parent = n1
    sess.add_all([n1, n2])
    sess.commit()
    n1.parent.name, n2.parent.name
    n1.children, n2.children

    copied = copy_graph(n1)
    assert copied is not n1
    assert copied.parent is not n2
    assert copied.parent.parent is copied
    assert copied.parent.children[0] is copied
    assert copied.children[0] is copied.parent

    other = Session(sess.bind)
    other.add(copied)
    other.flush()
    assert other.query(Node).count() == 4
    assert copied.parent_id == copied.parent.id
    other.close()


def test_self_reference_tree(session):
    sess, _ = session
    root = Node(name="root")
    child = Node(name="child", parent=root)
    grandchild = Node(name="grandchild", parent=child)
    sess.add(root)
    sess.commit()
    for node in (root, child, grandchild):
        node.parent, node.parent_id, node.children

    copied = copy_graph(root)
    assert copied.name == "root"
    assert copied.children[0].name == "child"
    assert copied.children[0].children[0].name == "grandchild"
    assert copied.children[0].parent is copied
    assert copied.children[0] is not child


def test_generated_values_not_copied(session):
    _, release = session
    copied = copy_graph(release)

    assert copied.id is None
    assert copied.version_id is None
    assert copied.created_at is None
    assert copied.name == "v1-config"
    assert copied.notes == "secret notes"


def test_joined_table_inheritance(session):
    _, release = session
    copied = copy_graph(release)

    kinds = {type(s) for s in copied.sections}
    assert Section in kinds
    assert AdvancedSection in kinds
    advanced = next(
        s for s in copied.sections if isinstance(s, AdvancedSection)
    )
    assert advanced.extra == "adv-data"
    assert advanced.id is None


def test_cross_session_persist(session, engine):
    sess, release = session
    old_release_id = release.id
    old_tag_ids = {t.id for t in sess.query(Tag)}

    copied = copy_graph(release)
    other = Session(engine)
    other.add(copied)
    other.flush()

    # generated values came from the database
    assert copied.id is not None and copied.id != old_release_id
    assert copied.version_id == 1
    assert copied.created_at is not None

    # FK of composite-PK children re-pointed at the new release
    items = copied_items(copied)
    assert set(items) == {"A", "B"}
    for item in items.values():
        assert item.release_id == copied.id
        assert other.get(Item, (copied.id, item.code)) is item

    # joined-table inheritance rows landed in both tables
    assert other.query(Section).count() == 4
    assert other.query(AdvancedSection).count() == 2

    # many-to-many: new tag rows + new association rows for the new items
    new_tags = other.query(Tag).filter(~Tag.id.in_(old_tag_ids)).all()
    assert len(new_tags) == 2
    assoc_rows = other.execute(item_tag.select()).all()
    assert len(assoc_rows) == 6  # 3 original + 3 copied
    new_assoc = [r for r in assoc_rows if r.item_release_id == copied.id]
    assert len(new_assoc) == 3

    # traversed owner was copied as well
    assert other.query(User).count() == 2

    other.commit()
    other.close()

    # source rows are untouched
    fresh = Session(engine)
    assert fresh.get(Release, old_release_id).name == "v1-config"
    assert fresh.query(Release).count() == 2
    old_items = fresh.query(Item).filter_by(release_id=old_release_id).all()
    assert {i.code for i in old_items} == {"A", "B"}
    fresh.close()


def test_keep_policy_and_session_constraint(session, engine):
    sess, release = session
    owner = release.items[0].owner

    copied = copy_graph(release, policies={"Item.owner": KEEP})
    for item in copied.items:
        assert item.owner is owner

    # adding to the same session is fine and reuses the original row
    sess.add(copied)
    sess.flush()
    assert all(i.owner_id == owner.id for i in copied.items)
    assert sess.query(User).count() == 1
    sess.rollback()
    fully_load(release)

    # ... but a *different* session cannot adopt the still-attached
    # original referenced by the KEEP policy
    copied2 = copy_graph(release, policies={"Item.owner": KEEP})
    other = Session(engine)
    with pytest.raises(InvalidRequestError):
        other.add(copied2)
        other.flush()
    other.close()

    # detaching the original first lifts the constraint
    copied3 = copy_graph(release, policies={"Item.owner": KEEP})
    sess.expunge(owner)
    other2 = Session(engine)
    other2.add(copied3)
    other2.flush()
    assert other2.query(User).count() == 1
    assert all(i.owner_id == owner.id for i in copied3.items)
    other2.close()


def test_ignore_policy(session):
    sess, release = session
    copied = copy_graph(release, policies={"Item.tags": IGNORE})

    assert all(item.tags == [] for item in copied.items)
    sess.add(copied)
    sess.flush()
    # no tags were copied and no new association rows were written
    assert sess.query(Tag).count() == 2
    assert len(sess.execute(item_tag.select()).all()) == 3


def test_unloaded_attributes_fail_without_sql(session, engine):
    sess, release = session
    sess.expire_all()

    with SQLCounter(engine) as counter:
        with pytest.raises(UnloadedAttributeError) as excinfo:
            copy_graph(release)
    assert counter.count == 0

    paths = excinfo.value.paths
    assert "Release.notes" in paths
    assert "Release.sections" in paths
    assert "Release.items" in paths

    # a single expired attribute is reported on its own
    fully_load(release)
    sess.expire(release, ["notes"])
    with pytest.raises(UnloadedAttributeError) as excinfo:
        copy_graph(release)
    assert excinfo.value.paths == ("Release.notes",)

    # after explicit loading the copy succeeds
    fully_load(release)
    assert copy_graph(release).name == "v1-config"


def test_copy_emits_zero_sql(session, engine):
    _, release = session
    with SQLCounter(engine) as counter:
        copy_graph(release)
    assert counter.count == 0


def test_on_unloaded_skip(session, engine):
    sess, release = session
    sess.expire(release, ["notes"])

    with SQLCounter(engine) as counter:
        copied = copy_graph(release, on_unloaded="skip")
    assert counter.count == 0
    # the skipped attribute was left unset on the copy
    assert "notes" in inspect(copied).unloaded
    assert copied.name == release.name


def test_same_row_two_sessions_copied_once(session, engine):
    sess, release = session
    other = Session(engine)
    same_release = other.get(Release, release.id)
    fully_load(same_release)

    copied_a, copied_b = copy_graph([release, same_release])
    assert copied_a is copied_b
    other.close()


def test_source_state_unchanged(session):
    sess, release = session

    # make the source dirty first, to prove the copy does not disturb it
    release.name = "v1-config-edited"
    dirty_before = set(sess.dirty)
    new_before = set(sess.new)
    history_before = inspect(release).attrs.name.history
    sections_history_before = inspect(release).attrs.sections.history
    identity_before = inspect(release).identity

    copy_graph(release)

    assert inspect(release).identity == identity_before
    assert inspect(release).attrs.name.history == history_before
    assert inspect(release).attrs.sections.history == sections_history_before
    assert set(sess.dirty) == dirty_before
    assert set(sess.new) == new_before
    assert sess.is_modified(release, include_collections=True)
    # the copied value was taken, the source object itself is unchanged
    assert release.name == "v1-config-edited"


def test_composite_key_remapped_on_flush(session, engine):
    sess, release = session
    copied = copy_graph(release)

    items = copied_items(copied)
    # natural key component copied, generated/FK component reset
    assert items["A"].code == "A"
    assert items["A"].release_id is None

    other = Session(engine)
    other.add(copied)
    other.flush()
    assert (items["A"].release_id, items["A"].code) == (copied.id, "A")
    assert other.get(Item, (copied.id, "A")) is items["A"]
    # original composite key rows still exist untouched
    assert other.get(Item, (release.id, "A")) is not items["A"]
    other.close()
