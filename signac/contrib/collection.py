# Copyright (c) 2017 The Regents of the University of Michigan
# All rights reserved.
# This software is licensed under the BSD 3-Clause License.

# The API of the Collection class is adapted from the API of
# the pymongo.Collection class provided as part of the
# mongo-python-driver library [1], which is licensed under the
# Apache License 2.0.
#
# The implementation found here is designed to replicate a subset
# of the behavior of a pymongo.Collection while operating directly
# on files on the local file system instead of a MongoDB database.
#
# [1]: https://github.com/mongodb/mongo-python-driver
import sys
import io
import re
import logging
import warnings
import argparse
import operator
from collections import defaultdict
from itertools import islice
from uuid import uuid4

from ..core.json import json
from ..common import six
from .filterparse import parse_filter_arg
if six.PY2:
    from collections import Mapping
else:
    from collections.abc import Mapping


logger = logging.getLogger(__name__)


_INDEX_OPERATORS = ('$eq', '$gt', '$gte', '$lt', '$lte', '$ne',
                    '$in', '$nin', '$regex', '$type', '$where')

_TYPES = {
    'int': int,
    'float': float,
    'bool': bool,
    'str': basestring if six.PY2 else str,   # noqa
    'list': tuple,
    'null': type(None),
}


def _index(docs, key):
    return {doc[key]: doc for doc in docs}


def _flatten(container):
    for i in container:
        if isinstance(i, (list, tuple)):
            for j in _flatten(i):
                yield j
        else:
            yield i


def _to_tuples(l):
    if isinstance(l, list):
        return tuple(_to_tuples(_) for _ in l)
    else:
        return l


class _DictPlaceholder(object):
    pass


def _encode_tree(x):
    if isinstance(x, list):
        return _to_tuples(x)
    else:
        return x


def _traverse_tree(t, encode=None, key=None):
    if encode is not None:
        t = encode(t)
    if isinstance(t, Mapping):
        if t:
            for k in t:
                k_ = k if key is None else '.'.join((key, k))
                for k__, v in _traverse_tree(t[k], key=k_, encode=encode):
                    yield k__, v
        elif key is not None:
            yield key, t
    else:
        yield key, t


def _traverse_filter(t):
    for key, value in _traverse_tree(t, encode=_encode_tree):
        yield key, value


def _valid_filter(f, top=True):
    if isinstance(f, Mapping):
        return all(_valid_filter(v, top=False) for v in f.values())
    elif isinstance(f, list):
        return not top
    else:
        return True


def _build_index(docs, key, primary_key):
    nodes = key.split('.')
    index = defaultdict(set)

    def _get_value(doc_, nodes):
        if nodes:
            if isinstance(doc_, dict):
                return _get_value(doc_[nodes[0]], nodes[1:])
            else:
                raise KeyError()
        else:
            return doc_

    for doc in docs:
        try:
            v = _get_value(doc, nodes)
            if isinstance(v, dict):
                v = _DictPlaceholder
        except KeyError:
            pass
        except Exception as error:
            raise RuntimeError(
                "An exepected error occured while processing "
                "doc '{}': {}.".format(doc, error))
        else:
            index[_encode_tree(v)].add(doc[primary_key])

        if len(nodes) > 1:
            try:
                v = doc['.'.join(nodes)]
            except KeyError:
                pass
            else:
                warnings.warn(
                    "Using keys with dots ('.') is pending deprecation in the future!",
                    PendingDeprecationWarning)
                index[_encode_tree(v)].add(doc[primary_key])
    return index


def _find_with_index_operator(index, op, argument):
    if op == '$in':
        def op(value, argument):
            return value in argument
    elif op == '$nin':
        def op(value, argument):
            return value not in argument
    elif op == '$regex':
        def op(value, argument):
            if isinstance(value, basestring if six.PY2 else str):  # noqa
                return re.search(argument, value)
            else:
                return False
    elif op == '$type':
        def op(value, argument):
            if argument in _TYPES:
                t = _TYPES[argument]
            else:
                raise ValueError("Unknown argument for $type operator: '{}'.".format(argument))
            return isinstance(value, t)
    elif op == '$where':
        def op(value, argument):
            return eval(argument)(value)
    else:
        op = getattr(operator, {'$gte': '$ge', '$lte': '$le'}.get(op, op)[1:])
    matches = set()
    for value in index:
        if op(value, argument):
            matches.update(index[value])
    return matches


def _check_logical_operator_argument(op, argument):
    if not isinstance(argument, list):
        raise ValueError("The argument of logical-operator '{}' must be a list!".format(op))
    if not len(argument):
        raise ValueError("The argument of logical-operator '{}' cannot be empty!".format(op))


class _CollectionSearchResults(object):
    "Iterator for a Collection result vector."

    def __init__(self, collection, _ids):
        self._collection = collection
        self._ids = _ids

    def __iter__(self):
        return (self._collection[_id] for _id in self._ids)

    def __len__(self):
        return len(self._ids)

    count = __len__


class Collection(object):
    """A collection of documents.

    The Collection class manages a collection of documents in memory
    or in a file on disk. A document is defined as a dictionary mapping
    of key-value pairs.

    An instance of collection may be used to manage and search documents.
    For example, given a collection with member data, where each document
    contains a `name` entry and an `age` entry, we can find the name of
    all members that are at age 32 like this:

    .. code-block:: python

        members = [
            {'name': 'John',  'age': 32},
            {'name': 'Alice', 'age': 28},
            {'name': 'Kevin', 'age': 32},
            # ...
            ]

        member_collection = Collection(members)
        for doc in member_collection.find({'age': 32}):
            print(doc['name'])

    To iterate over all documents in the collection, use:

    .. code-block:: python

        for doc in collection:
            print(doc)

    By default a collection object will reside in memory. However, it is
    possible to manage a collection associated to a file on disk. To open
    a collection which is associated with a file on disk, use the
    :py:meth:`.open` class method:

    .. code-block:: python

        with Collection.open('collection.txt') as collection:
            for doc in collection.find({'age': 32}):
                print(doc)

    The collection file is by default opened in `a+` mode, which means it can
    be read from and written to and will be created if it does not exist yet.

    :param docs: Initialize the collection with these documents.
    :param primary_key: The name of the key which serves as the primary
        index of the collection. Selecting documents by primary key has
        time complexity of O(N) in the worst case and O(1) on average.
        All documents must have a primary key value. The default primary
        key is `_id`.
    """

    def __init__(self, docs=None, primary_key='_id'):
        self._primary_key = primary_key
        self._file = io.StringIO()
        self._requires_flush = False
        self._dirty = set()
        self._indexes = dict()
        self._docs = dict()
        if docs is not None:
            for doc in docs:
                self[doc[self.primary_key]] = doc
            self._requires_flush = False  # not needed after initial read!
            self._update_indexes()

    def _assert_open(self):
        if self._docs is None:
            raise RuntimeError("Trying to access closed {}.".format(
                type(self).__name__))

    def _remove_from_indexes(self, _id):
        for index in self._indexes.values():
            remove_keys = set()
            for key, group in index.items():
                try:
                    group.remove(_id)
                except KeyError:
                    pass
                if not len(group):
                    remove_keys.add(key)
            for key in remove_keys:
                del index[key]

    def _update_indexes(self):
        if self._dirty:
            for _id in self._dirty:
                self._remove_from_indexes(_id)
            docs = [self[_id] for _id in self._dirty]
            for key, index in self._indexes.items():
                tmp = _build_index(docs, key, self.primary_key)
                for v, group in tmp.items():
                    index[v].update(group)
            self._dirty.clear()

    def _build_index(self, key):
        logger.debug("Building index for key '{}'...".format(key))
        self._indexes[key] = _build_index(self._docs.values(), key, self.primary_key)
        logger.debug("Built index for key '{}'.".format(key))

    def index(self, key, build=False):
        """Get (and optionally build) the index for a given key.

        An index allows to access documents by a specific key with
        minimal time complexity, e.g.:

        .. code-block:: python

            age_index = member_collection.index('age')
            for _id in age_index[32]:
                print(member_collection[_id]['name'])

        This means we can access documents by the 'age' key in O(1) time on
        average in addition to the primary key. Using the :py:meth:`.find`
        method will automatically build all required indexes for the particular
        search.

        Once an index has been built, it will be internally managed by the
        class and updated with subsequent changes. An index returned by this
        method is always current with the latest state of the collection.

        :param key: The primary key of the requested index.
        :type key: str
        :param build: If True, build a non-existing index if necessary,
            otherwise raise KeyError.
        :raises KeyError: In case that build is False and the index has not
            been built yet.
        """
        if key == self.primary_key:
            raise KeyError("Can't access index for primary key via index() method.")
        elif key not in self._indexes:
            if build:
                self._build_index(key)
            else:
                raise KeyError("No index for key '{}'.".format(key))
        self._update_indexes()
        return self._indexes[key]

    def __str__(self):
        return "<{} file={}>".format(type(self).__name__, self._file)

    def __iter__(self):
        self._assert_open()
        return iter(self._docs.values())

    @property
    def ids(self):
        "Return an iterator over the primary key in the collection."
        self._assert_open()
        return iter(self._docs)

    @property
    def primary_key(self):
        "The name of the collection's primary key (default='_id')."
        return self._primary_key

    def __len__(self):
        self._assert_open()
        return len(self._docs)

    def __contains__(self, _id):
        return _id in self._docs

    def __getitem__(self, _id):
        self._assert_open()
        return self._docs[_id].copy()

    def __setitem__(self, _id, doc):
        self._assert_open()
        if six.PY2:
            if not isinstance(_id, basestring):  # noqa
                raise TypeError("The primary key must be of type str!")
        else:
            if not isinstance(_id, str):
                raise TypeError("The primary key must be of type str!")
        doc.setdefault(self.primary_key, _id)
        if _id != doc[self.primary_key]:
            raise ValueError("Primary key mismatch!")
        self._docs[_id] = json.loads(json.dumps(doc))
        self._dirty.add(_id)
        self._requires_flush = True

    def insert_one(self, doc):
        """Insert one document into the collection

        If the document does not have a value for the
        collection's primary key yet, it will be assigned one.

        .. code-block:: python

            _id = collection.insert_one(doc)
            assert _id in collection

        .. note::

            The document will be directly updated in case that
            it has no primary key and must therefore be mutable!

        :param doc: The document to be inserted.
        :returns: The _id of the inserted documented.
        """
        self._assert_open()
        _id = doc.setdefault(self.primary_key, str(uuid4()))
        self[_id] = doc
        return _id

    def __delitem__(self, _id):
        self._assert_open()
        del self._docs[_id]
        self._remove_from_indexes(_id)
        try:
            self._dirty.remove(_id)
        except KeyError:
            pass
        self._requires_flush = True

    def clear(self):
        "Remove all documents from the collection."
        self._docs.clear()
        self._indexes.clear()
        self._dirty.clear()
        self._requires_flush = True

    def update(self, docs):
        """Update the collection with these documents.

        Any existing documents with the same primary key
        will be replaced.

        :param docs: A sequence of documents to be upserted
            into the collection.
        """
        for doc in docs:
            doc.setdefault(self.primary_key, str(uuid4()))
            self[doc[self.primary_key]] = doc

    def _check_filter(self, filter):
        "Check if filter is a valid filter argument."
        if filter is None:
            return True
        if not _valid_filter(filter):
            raise ValueError(filter)

    def _find_expression(self, key, value):
        logger.debug("Find documents for expression '{}: {}'.".format(key, value))
        if '$' in key:
            if key.count('$') > 1:
                raise KeyError("Bad operator expression '{}'.".format(key))
            nodes = key.split('.')
            op = nodes[-1]
            if not op.startswith('$'):
                raise KeyError("Bad operator placement '{}'.".format(key))
            key = '.'.join(nodes[:-1])
            if op in _INDEX_OPERATORS:
                index = self.index(key, build=True)
                return _find_with_index_operator(index, op, value)
            elif op == '$exists':
                if not isinstance(value, bool):
                    raise ValueError("The value of the '$exists' operator must be boolean.")
                index = self.index(key, build=True)
                match = {elem for elems in index.values() for elem in elems}
                return match if value else set(self.ids).difference(match)
            else:
                raise KeyError("Unknown expression-operator '{}'.".format(op))
        else:
            index = self.index(key, build=True)
            return index.get(value, set())

    def _find_result(self, expr):
        result = None
        if not len(expr):
            return set(self.ids)    # Empty expression yields all ids...
        else:
            result = None

        def _reduce_result(result, match):
            if result is None:  # First match
                return match
            else:               # Update previous match
                return result.intersection(match)

        # Check if filter contains primary key, in which case we can
        # immediately reduce the result.
        _id = expr.pop(self.primary_key, None)
        if _id is not None and _id in self:
            result = _reduce_result(result, {_id})

        # Extract all logical-operator expressions for now.
        or_expressions = expr.pop('$or', None)
        and_expressions = expr.pop('$and', None)
        not_expression = expr.pop('$not', None)

        # Reduce the result based on the remaining non-logical expression:
        for key, value in _traverse_filter(expr):
            result = _reduce_result(result, self._find_expression(key, value))
            if not result:          # No match, no need to continue...
                return set()

        # Reduce the result based on the logical-operator expressions:
        if not_expression is not None:
            not_match = self._find_result(not_expression)
            result = _reduce_result(result, set(self.ids).difference(not_match))

        if and_expressions is not None:
            _check_logical_operator_argument('$and', and_expressions)
            for expr_ in and_expressions:
                result = _reduce_result(result, self._find_result(expr_))

        if or_expressions is not None:
            _check_logical_operator_argument('$or', or_expressions)
            or_results = set()
            for expr_ in or_expressions:
                or_results.update(self._find_result(expr_))
            result = _reduce_result(result, or_results)

        assert result is not None
        return result

    def _find(self, filter=None, limit=0):
        """Returns a result vector of ids for the given filter and limit.

        This function normalizes the filter argument and then attempts to
        build a result vector for the given key-value queries.
        For each key that is queried, an internal index is built and then
        searched.

        The result vector is a set of ids, where each id is the value of the
        primary key of a document that matches the given filter.

        The _find() method uses the following optimizations:

            1. If the filter is None, the result is directly returned, since
               all documents will match an empty filter.
            2. If the filter argument contains a primary key, the result
               is directly returned since no search operation is necessary.
            3. The filter is processed key by key, once the result vector is
               empty it is immediately returned.

        :param filter: The filter argument that all documents must match.
        :param limit: Limit the size of the result vector.
        :raises ValueError: In case that the filter argument is invalid.
        :returns: A set of ids of documents that match the given filter.
        """
        self._assert_open()
        filter = json.loads(json.dumps(filter))  # Normalize
        self._check_filter(filter)
        if filter is None or not len(filter):
            return set(islice(self._docs.keys(), limit if limit else None))
        result = self._find_result(filter)
        return set(islice(result, limit if limit else None))

    def find(self, filter=None, limit=0):
        """Find all documents matching filter, but not more than limit.

        This function searches the collection for all documents that match
        the given filter and returns a result vector. For example:

        .. code-block:: python

            for doc in collection.find(my_filter):
                print(doc)

        Nested values should be searched using the ``.`` operator, for example:

        .. code-block:: python

            docs = collection.find({'nested.value': 42})

        will return documents with a nested structure: ``{'nested': {'value': 42}}``.

        The result of :py:meth:`~.find` can be stored and iterated over multiple times.
        In addition, the result vector can be queried for its size:

        .. code-block:: python

            docs = collection.find(my_filter)

            print(len(docs))    # the number of documents matching

            for doc in docs:    # iterate over the result vector
                pass

        Arithmetic Operators

                * *$eq*: equal
                * *$neq*: not equal
                * *$gt*: greater than
                * *$gte*: greater or equal than
                * *$lt*: less than
                * *$lte*: less or equal than

            .. code-block:: python

                project.find({"a": {"$lt": 5})

            Matches all docs with *a* less than 5.

        Logical Operators

            That includes *$and* and *$or*; both expect a list of expressions.

                .. code-block:: python

                    project.find({"$or": [{"a": 4}, {"b": {"$gt": 3}}]})

            Matches all docs, where *a* is 4 or *b* is greater than 3.

        Exists operator

            Determines whether a specific key exists, or not, e.g.:

                    .. code-block:: python

                        project.find({"a": {"$exists": True}})

        Array operator

            To determine whether specific elements are in (*$in*), or not in (*$nin*)
            an array, e.g.:

                    .. code-block:: python

                        project.find({"a": {"$in": [0, 1, 2]}})

            Matches all docs, where *a* is either 0, 1, or 2. Usage of *$nin* is equivalent.

        Regular expression operator

            Allows the "on-the-fly" evaluation of regular expressoions, e.g.:

                    .. code-block:: python

                        project.find({"protocol": {"$regex": "foo"}})

            Will match all docs with a protocol that contains the term 'foo'.

        $type operator

            Matches when a value is of specific type, e.g.:

                    .. code-block:: python

                        project.find({"protocol": {"$type": str}})

            Finds all docs, where the value of protocol is of type str.
            Other types that can be checked are: *int*, *float*, *bool*, *list*, and *null*.

        $where operator

            Matches an arbitrary python expression, e.g.:

                    .. code-block:: python

                        project.find({"foo": {"$where": "lambda x: x.startswith('bar')"}})

            Matches all docs, where the value for foo starts with the word 'bar'.

        :param filter: All documents must match the given filter.
        :type filter: Mapping
        :param limit: Do not return more than limit number of documents.
            A limit value of 0 (the default) means no limit.
        :type limit: int
        :returns: A result object that iterates over all matching documents.
        :raises ValueError: In case that the filter argument is invalid.
        """
        return _CollectionSearchResults(self, self._find(filter, limit=limit))

    def find_one(self, filter=None):
        """Return one document that matches the filter or None.

        .. code-block:: python

            doc = collection.find_one(my_filter)
            if doc is None:
                print("No result found for filter", my_filter)
            else:
                print("Doc matching filter:", my_filter, doc)

        :param filter: The returned document must match the given filter.
        :raises ValueError: In case that the filter argument is invalid.
        :returns: A matching document or None.
        """
        for doc in self.find(filter, limit=1):
            return doc

    def replace_one(self, filter, replacement, upsert=False):
        """Replace one document that matches the given filter.

        The first document matching the filter will be replaced
        by the given replacement document. If the `upsert` argument
        is True, the replacement will be inserted in case that
        no document matches the filter.

        :param filter: A document that should be replaced must
            match this filter.
        :param replacement: The replacement document.
        :param upsert: If True, insert the replacement document in
            the case that no document matches the filter.
        :raises ValueError: In case that the filter argument is invalid.
        :returns: The _id of the replaced (or upserted) documented.
        """
        self._assert_open()
        if len(filter) == 1 and self.primary_key in filter:
            _id = filter[self.primary_key]
            if upsert or _id in self:
                self[_id] = replacement
        else:
            for _id in self._find(filter):
                self[_id] = replacement
                return _id
            else:
                if upsert:
                    return self.insert_one(replacement)

    def delete_many(self, filter):
        "Delete all documents that match the filter."
        to_delete = set(self._find(filter))
        for _id in to_delete:
            del self[_id]

    def delete_one(self, filter):
        "Delete one document that matches the filter."
        to_delete = set(self._find(filter, limit=1))
        for _id in to_delete:
            del self[_id]

    def dump(self, file=sys.stdout):
        """Dump the collection in JSON-encoding to file.

        The file argument defaults to `sys.stdout`, which means
        the encoded blob will be printed to screen in case
        that no file argument is provided.

        For example, to dump to a file on disk, one could write:

        .. code-block:: python

            with open('my_collection.txt', 'w') as file:
                collection.dump(file)

        :param file: The file to write the encoded blob to.
        """
        self._assert_open()
        for doc in self._docs.values():
            file.write(json.dumps(doc) + '\n')

    @classmethod
    def _open(cls, file):
        try:
            docs = (json.loads(line) for line in file)
            collection = cls(docs=docs)
        except (IOError, io.UnsupportedOperation):
            collection = cls()
        collection._file = file
        return collection

    @classmethod
    def open(cls, filename, mode='a+'):
        """Open a collection associated with a file on disk.

        Using this factory method will return a collection that is
        associated with a collection file on disk. For example:

        .. code-block:: python

            with Collection.open('collection.txt') as collection:
                for doc in collection:
                    print(doc)

        will read all documents from the `collection.txt` file or create
        the file if it does not exist yet.

        Modifications to the file will be written to the file when the
        :py:meth:`.flush` method is called or the collection is explicitly
        closed by calling the :py:meth:`.close` method or implicitly by
        leaving the `with`-clause:

        .. code-block:: python

            with Collection.open('collection.txt') as collection:
                collection.update(my_docs)
            # All changes to the collection have been written to collection.txt.

        The open-modes work as expected, so for example to open a collection
        file in *read-only* mode, use ``Collection.open('collection.txt', 'r')``.
        """
        logger.debug("Open collection '{}'.".format(filename))
        if filename == ':memory:':
            file = io.StringIO()
        else:
            file = open(filename, mode)
            file.seek(0)
        return cls._open(file)

    def flush(self):
        """Write all changes to the associated file.

        If the collection instance is associated with a file-object,
        calling the :py:meth:`~.flush` method will write all changes to this file.

        This method is also called when the collection is explicitly or
        implicitly closed.
        """
        self._assert_open()
        if self._requires_flush:
            if self._file is None:
                logger.debug("Flushed collection.")
            else:
                logger.debug("Flush collection to file '{}'.".format(self._file))
                self._file.truncate()
                self.dump(self._file)
                self._file.flush()
            self._requires_flush = False
        else:
            logger.debug("Flushed collection (no changes).")

    def close(self):
        """Close this collection instance.

        In case that the collection is associated with a file-object,
        all changes are flushed to the file and the file is closed.

        It is not possible to re-open the same collection instance
        after closing it.
        """
        if self._file is not None:
            try:
                self.flush()
            finally:
                self._file.close()
                self._indexes.clear()
                self._docs = None
                self._file = None

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        self.close()

    def main(self):
        """Start a command line interface for this Collection.

        Use this function to interact with this instance of Collection
        on the command line. For example, executing the following script:

        .. code-block:: python

            # find.py
            with Collection.open('my_collection.txt') as c:
                c.main()

        will enable us to search for documents on the command line like this:

        .. code-block:: bash

            $ python find.py '{"age": 32}'
            {"name": "John", "age": 32}
            {"name": "Kevin", "age": 32}

        """
        parser = argparse.ArgumentParser(
            "Command line interface for instances of Collection.")
        parser.add_argument(
            'filter',
            nargs='*',
            help="The search filter provided in JSON encoding. "
                 "Leave empty to return all documents.")
        parser.add_argument(
            '-l', '--limit',
            type=int,
            default=0,
            help="Limit the number of search results that are "
                 "maximally returned. A value of 0 (the default) "
                 "means no limit.")
        parser.add_argument(
            '--id',
            dest='_id',
            action='store_true',
            help="Print a document's primary key instead of the whole document.")
        parser.add_argument(
            '-i', '--indent',
            action='store_true',
            help="Print results in indented format.")
        args = parser.parse_args()
        if args._id and args.indent:
            raise ValueError("Select either `--id` or `--indent`, not both.")
        f = parse_filter_arg(args.filter)
        for doc in self.find(f, limit=args.limit):
            if args._id:
                print(doc[self.primary_key])
            else:
                if args.indent:
                    print(json.dumps(doc, indent=2))
                else:
                    print(json.dumps(doc))