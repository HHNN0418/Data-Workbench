"""Role memory read field by field from its own rows (D89).

Role memory persists one meta row per field, memory/<role>/<field>: 13 MB of the
19 MB agent store for 29 roles on 23 Oct 2026. Every full read and write used to
decode all of it, mostly while holding the writer lock every role queues
behind, though a round touches a few fields of one role.

LazyMemory behaves like the old {role: {field: value}} dict. A field row is
decoded the first time it is used, and a write stores only the fields that
were loaded, assigned or deleted. Iterating roles or a role's fields needs no
field values: names come from the index rows read with the rest of the store.
"""
from __future__ import annotations

import copy
import json
from collections.abc import KeysView

PREFIX = 'agent_control_v1:'
_MISSING = object()


def field_key(role, field):
    return 'memory/' + role + '/' + field


def _ordered(listed, present):
    """The order _assemble gives: listed names first, then the rest sorted."""
    order = [name for name in listed if name in present]
    return order + sorted(set(present) - set(order))


class LazyRole(dict):
    """One role's memory. The dict storage holds only the fields loaded so far."""

    def __init__(self, owner, role, names):
        super().__init__()
        self._owner, self._role = owner, role
        self._names = list(names)
        self._deleted = set()

    def _ensure(self, fields):
        missing = [field for field in fields
                   if field in self._names and not dict.__contains__(self, field)]
        if missing:
            found = self._owner.fetch(self._role, missing)
            for field in missing:
                if field in found:
                    dict.__setitem__(self, field, found[field])
                else:  # removed since the index was read
                    self._names.remove(field)

    def _pairs(self):
        self._ensure(self._names)
        return [(field, dict.__getitem__(self, field)) for field in self._names]

    def __getitem__(self, key):
        self._ensure([key])
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._ensure([key])
        return dict.get(self, key, default)

    def __contains__(self, key):
        return key in self._names

    def __iter__(self):
        return iter(list(self._names))

    def __len__(self):
        return len(self._names)

    def keys(self):
        return KeysView(self)

    def items(self):
        return self._pairs()

    def values(self):
        return [value for _, value in self._pairs()]

    def __setitem__(self, key, value):
        if key not in self._names:
            self._names.append(key)
        self._deleted.discard(key)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        if key not in self._names:
            raise KeyError(key)
        self._names.remove(key)
        self._deleted.add(key)
        dict.pop(self, key, None)

    def pop(self, key, default=_MISSING):
        if key in self._names:
            value = self[key]
            del self[key]
            return value
        if default is _MISSING:
            raise KeyError(key)
        return default

    def popitem(self):
        if not self._names:
            raise KeyError('popitem(): dictionary is empty')
        key = self._names[-1]
        return key, self.pop(key)

    def setdefault(self, key, default=None):
        if key in self._names:
            return self[key]
        self[key] = default
        return default

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def clear(self):
        for key in list(self._names):
            del self[key]

    def copy(self):
        return dict(self._pairs())

    __copy__ = copy

    def __deepcopy__(self, memo):
        return copy.deepcopy(dict(self._pairs()), memo)

    def detached(self):
        """An independent copy that still loads lazily: fields loaded here are
        deep-copied, the rest decode from their rows when first used."""
        twin = LazyRole(self._owner, self._role, self._names)
        for field in self._names:
            if dict.__contains__(self, field):
                dict.__setitem__(twin, field, copy.deepcopy(dict.__getitem__(self, field)))
        return twin

    def __reduce_ex__(self, protocol):
        return dict, (self._pairs(),)

    def __eq__(self, other):
        return dict(self._pairs()) == (other.copy() if isinstance(other, (LazyRole, LazyMemory)) else other)

    def __ne__(self, other):
        return not self == other

    __hash__ = None

    def __or__(self, other):
        return {**self.copy(), **other}

    def __ror__(self, other):
        return {**other, **self.copy()}

    def __ior__(self, other):
        self.update(other)
        return self

    def __repr__(self):
        return repr(dict(self._pairs()))


class LazyMemory(dict):
    """Role id -> LazyRole, or the plain value a caller assigned in its place."""

    def __init__(self, fetch_rows, roles, persisted):
        """``roles``: ordered {role: ordered field names}; ``fetch_rows(keys)``
        returns {key: raw JSON} for the existing keys (without PREFIX)."""
        super().__init__()
        self._fetch_rows = fetch_rows
        self._names = list(roles)
        self._fields = {role: list(fields) for role, fields in roles.items()}
        self._persisted = persisted
        self._assigned = set()
        self._deleted = set()

    def fetch(self, role, fields):
        names = [field_key(role, field) for field in fields]
        raw = self._fetch_rows(names)
        values = {}
        for field, name in zip(fields, names):
            if name not in raw:
                continue
            try:
                values[field] = json.loads(raw[name])
            except (ValueError, TypeError):
                from .agent_control import AgentError
                raise AgentError('Agent 状态无法读取，请检查数据库备份', 500) from None
            if self._persisted is not None:
                self._persisted[name] = raw[name]
        return values

    def _role(self, role):
        value = dict.get(self, role, _MISSING)
        if value is _MISSING:
            value = LazyRole(self, role, self._fields.get(role, []))
            dict.__setitem__(self, role, value)
        return value

    def loaded_items(self):
        """Roles whose memory this call has touched (accessed or assigned)."""
        return [(role, dict.__getitem__(self, role)) for role in self._names if dict.__contains__(self, role)]

    def __getitem__(self, key):
        if key not in self._names:
            raise KeyError(key)
        return self._role(key)

    def get(self, key, default=None):
        return self._role(key) if key in self._names else default

    def __contains__(self, key):
        return key in self._names

    def __iter__(self):
        return iter(list(self._names))

    def __len__(self):
        return len(self._names)

    def keys(self):
        return KeysView(self)

    def items(self):
        return [(role, self._role(role)) for role in list(self._names)]

    def values(self):
        return [self._role(role) for role in list(self._names)]

    def __setitem__(self, key, value):
        if dict.get(self, key, _MISSING) is value:
            return
        if key not in self._names:
            self._names.append(key)
        self._deleted.discard(key)
        self._assigned.add(key)
        dict.__setitem__(self, key, value)

    def __delitem__(self, key):
        if key not in self._names:
            raise KeyError(key)
        self._names.remove(key)
        self._assigned.discard(key)
        self._deleted.add(key)
        dict.pop(self, key, None)

    def pop(self, key, default=_MISSING):
        if key in self._names:
            value = self[key]
            del self[key]
            return value
        if default is _MISSING:
            raise KeyError(key)
        return default

    def setdefault(self, key, default=None):
        if key in self._names:
            return self[key]
        self[key] = default
        return default

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def clear(self):
        for key in list(self._names):
            del self[key]

    def _plain(self):
        return {role: (value.copy() if isinstance(value, LazyRole) else value) for role, value in self.items()}

    def copy(self):
        return self._plain()

    __copy__ = copy

    def __deepcopy__(self, memo):
        return copy.deepcopy(self._plain(), memo)

    def __reduce_ex__(self, protocol):
        return dict, (list(self._plain().items()),)

    def __eq__(self, other):
        return self._plain() == (other._plain() if isinstance(other, LazyMemory) else other)

    def __ne__(self, other):
        return not self == other

    __hash__ = None

    def __repr__(self):
        return '<LazyMemory roles=%d loaded=%d>' % (len(self._names), len(self.loaded_items()))

    def store(self, put, delete):
        """Write the touched roles. ``put(name, value)`` skips unchanged JSON."""
        for role in self._deleted:
            delete('memory/' + role)
            for field in self._fields.get(role, []):
                delete(field_key(role, field))
        put('memory', list(self._names))
        for role, value in self.loaded_items():
            old = self._fields.get(role, [])
            if role in self._assigned and not isinstance(value, LazyRole):
                # A replaced role: the old layout of _flatten for this subtree.
                fields = list(value) if isinstance(value, dict) else []
                for field in old:
                    if field not in fields:
                        delete(field_key(role, field))
                put('memory/' + role, fields if isinstance(value, dict) else value)
                for field in fields:
                    put(field_key(role, field), value[field])
                continue
            for field in value._deleted:
                delete(field_key(role, field))
            put('memory/' + role, list(value._names))
            for field in value._names:
                if dict.__contains__(value, field):
                    put(field_key(role, field), dict.__getitem__(value, field))


def index(node_rows, field_names):
    """Ordered {role: [fields]} from the 'memory' and 'memory/<role>' rows and the
    names of existing field rows; None when a row still has the pre-split layout."""
    root = node_rows.get('memory')
    if root is not None and not isinstance(root, list):
        return None
    present = {}
    for name in node_rows:
        if name != 'memory':
            present.setdefault(name.split('/', 1)[1], set())
    for name in field_names:
        _, role, field = name.split('/', 2)
        present.setdefault(role, set()).add(field)
    roles = {}
    for role in _ordered(root or [], present):
        node = node_rows.get('memory/' + role)
        if node is not None and not isinstance(node, list):
            return None
        roles[role] = _ordered(node or [], present[role])
    return roles
