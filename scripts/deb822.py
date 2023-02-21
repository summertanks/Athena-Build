

class OrderedSet(object):
    """OrderedSet - Reused from debian.DEB822
                    Set is faster than list
    """

    def __init__(self, iterable: [str] = None):
        self.__set: set[str] = set()
        self.__order: [str] = []
        if iterable is None:
            iterable = []
        for item in iterable:
            self.add(item)

    def add(self, item):
        # item is assumed hashable, otherwise set() will auto raise error
        if item not in self:
            self.__set.add(item)
            self.__order.append(item)

    def remove(self, item: str):
        # assumed to exist, set.remove will else raise KeyError
        self.__set.remove(item)
        self.__order.remove(item)

    def __iter__(self) -> iter:
        # Return an iterator of items in the order they were added
        return iter(self.__order)

    def __len__(self) -> int:
        return len(self.__order)

    def __contains__(self, item) -> bool:
        # Lookup in a set is O(1) instead of O(n) for a list.
        return item in self.__set

    # ### list-like methods
    append = add

    def extend(self, iterable):
        for item in iterable:
            self.add(item)


class MutableClass:
    def __init__(self):
        self.__dict = {}
        self.__keys = OrderedSet()

    def __iter__(self):
        for key in self.__keys:
            yield str(key)

    def __len__(self):
        return len(self.__keys)

    def __setitem__(self, key, value):
        self.__keys.add(key)
        self.__dict[key] = value

    def __getitem__(self, key):
        try:
            value = self.__dict[key]
        except KeyError:
            value = ''
        return value

    def __delitem__(self, key):
        self.__keys.remove(key)
        try:
            del self.__dict[key]
        except KeyError:
            pass

    def __contains__(self, key):
        return key in self.__keys


class DEB822file(MutableClass):
    """DEB822 - Superclass to parse Deb822 Control files.
                This is Not a full-fledged RFC822 implementation,
                bare minimum to parse the Release, Package, Source & DSC file"""

    def __init__(self, section: str):

        super().__init__()

        # Save content for reference
        self.__raw = section

        # Parse as DEB822 file
        _lines = section.split('\n')

        current_field = None
        for _line in _lines:

            # Should not happen, sections are supposed to already be split '\n\n' and no line with spaces
            if _line.strip() == '':
                raise ValueError("ERROR: Attempting to create class with malformed section")

            if _line.startswith(' '):
                if current_field is None:
                    raise
                # This line is a continuation of the previous field, add '\n' if we need different fields from them
                self[current_field] += _line + '\n'
            else:
                # This line starts a new field
                current_field, value = _line.split(':', 1)
                self[current_field.strip()] = value.strip()

    @property
    def raw(self) -> str:
        return self.__raw
