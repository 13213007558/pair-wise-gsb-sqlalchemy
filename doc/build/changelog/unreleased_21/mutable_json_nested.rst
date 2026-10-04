.. change::
    :tags: feature, orm, ext

    Added :class:`.MutableJSON`, :class:`.MutableJSONDict`, and
    :class:`.MutableJSONList` for tracking in-place changes throughout nested
    JSON object and array structures.  Shared container references notify all
    associated ORM roots, path replacement and refresh/expiry detach only the
    removed edge, and container-to-container links use weak references so
    detached graphs do not keep recycled entities alive.
