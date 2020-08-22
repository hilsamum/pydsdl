#
# Copyright (C) 2018-2020  UAVCAN Development Team  <uavcan.org>
# This software is distributed under the terms of the MIT License.
#

import abc
import math
import typing
import itertools
from .. import _expression
from .. import _port_id_ranges
from .._bit_length_set import BitLengthSet
from ._serializable import SerializableType, TypeParameterError
from ._attribute import Attribute, Field, PaddingField, Constant
from ._name import check_name, InvalidNameError
from ._void import VoidType
from ._primitive import PrimitiveType, UnsignedIntegerType
try:
    from functools import cached_property
except ImportError:
    cached_property = property


Version = typing.NamedTuple('Version', [('major', int), ('minor', int)])


class InvalidVersionError(TypeParameterError):
    pass


class AttributeNameCollisionError(TypeParameterError):
    pass


class InvalidExtentError(TypeParameterError):
    pass


class InvalidFixedPortIDError(TypeParameterError):
    pass


class MalformedUnionError(TypeParameterError):
    pass


class DeprecatedDependencyError(TypeParameterError):
    pass


class CompositeType(SerializableType):
    """
    This is the most interesting type in the library because it represents an actual DSDL definition upon its
    interpretation.
    """

    MAX_NAME_LENGTH = 50
    MAX_VERSION_NUMBER = 255
    NAME_COMPONENT_SEPARATOR = '.'

    DEFAULT_DELIMITER_HEADER_BIT_LENGTH = 32

    def __init__(self,
                 name:             str,
                 version:          Version,
                 attributes:       typing.Iterable[Attribute],
                 extent:           typing.Optional[int],
                 final:            bool,
                 deprecated:       bool,
                 fixed_port_id:    typing.Optional[int],
                 source_file_path: str,
                 parent_service:   typing.Optional['ServiceType'] = None):
        super(CompositeType, self).__init__()

        self._name = str(name).strip()
        self._version = version
        self._attributes = list(attributes)
        self._attributes_by_name = {a.name: a for a in self._attributes if not isinstance(a, PaddingField)}
        self._final = bool(final)
        self._deprecated = bool(deprecated)
        self._fixed_port_id = None if fixed_port_id is None else int(fixed_port_id)
        self._source_file_path = str(source_file_path)
        self._parent_service = parent_service

        if self._parent_service is not None:
            assert self._name.endswith('.Request') or self._name.endswith('.Response')
            if not isinstance(self._parent_service, ServiceType):  # pragma: no cover
                raise ValueError('The parent service reference is invalid: %s' % type(parent_service).__name__)

        # Name check
        if not self._name:
            raise InvalidNameError('Composite type name cannot be empty')

        if self.NAME_COMPONENT_SEPARATOR not in self._name:
            raise InvalidNameError('Root namespace is not specified')

        # Do not check name length for synthesized types.
        if len(self._name) > self.MAX_NAME_LENGTH and parent_service is None:
            raise InvalidNameError('Name is too long: %r is longer than %d characters' %
                                   (self._name, self.MAX_NAME_LENGTH))

        for component in self._name.split(self.NAME_COMPONENT_SEPARATOR):
            check_name(component)

        # Version check
        version_valid = (0 <= self._version.major <= self.MAX_VERSION_NUMBER) and\
                        (0 <= self._version.minor <= self.MAX_VERSION_NUMBER) and\
                        ((self._version.major + self._version.minor) > 0)

        if not version_valid:
            raise InvalidVersionError('Invalid version numbers: %s.%s' % (self._version.major, self._version.minor))

        # Attribute check
        used_names = set()      # type: typing.Set[str]
        for a in self._attributes:
            if a.name and a.name in used_names:
                raise AttributeNameCollisionError('Multiple attributes under the same name: %r' % a.name)
            else:
                used_names.add(a.name)

        # Port ID check
        port_id = self._fixed_port_id
        if port_id is not None:
            assert port_id is not None
            if isinstance(self, ServiceType):
                if not (0 <= port_id <= _port_id_ranges.MAX_SERVICE_ID):
                    raise InvalidFixedPortIDError('Fixed service ID %r is not valid' % port_id)
            else:
                if not (0 <= port_id <= _port_id_ranges.MAX_SUBJECT_ID):
                    raise InvalidFixedPortIDError('Fixed subject ID %r is not valid' % port_id)

        # Consistent deprecation check.
        # A non-deprecated type cannot be dependent on deprecated types.
        # A deprecated type can be dependent on anything.
        if not self.deprecated:
            for a in self._attributes:
                t = a.data_type
                if isinstance(t, CompositeType):
                    if t.deprecated:
                        raise DeprecatedDependencyError('A type cannot depend on deprecated types '
                                                        'unless it is also deprecated.')

        # Extent initialization.
        minimal_extent = max(self._aggregate_bit_length_sets().pad_to_alignment(self.alignment_requirement))
        if extent is None:
            if self.final:
                self._extent = minimal_extent
            else:
                unaligned = max(self._aggregate_bit_length_sets()) * 2  # Multiply first, then align.
                self._extent = max(BitLengthSet(unaligned).pad_to_alignment(self.alignment_requirement))
        else:
            self._extent = int(extent)

        # Extent verification.
        assert self.alignment_requirement >= self.BITS_PER_BYTE
        assert self.alignment_requirement % self.BITS_PER_BYTE == 0
        if self._extent % self.alignment_requirement != 0:
            raise InvalidExtentError('The specified extent of %d bits is not a multiple of %d bits' %
                                     (self._extent, self.alignment_requirement))
        if self._extent < minimal_extent:
            raise InvalidExtentError(
                'The specified extent of %d bits is too small for this data type. '
                'Either compactify the data type or increase the extent at least to %d bits. '
                'Beware that the latter option may break wire compatibility.' %
                (self._extent, minimal_extent)
            )
        if self.final and self._extent > minimal_extent:
            raise InvalidExtentError(
                'Cannot override the extent because the type is final. '
                'Either remove the explicit extent specification or change it from the current value of '
                '%d bits to %d bits.' %
                (self._extent, minimal_extent)
            )
        # Paranoid internal consistency validation.
        assert self.extent % self.BITS_PER_BYTE == 0
        assert self.extent % self.alignment_requirement == 0
        assert self.extent >= minimal_extent
        assert self.extent == minimal_extent or not self.final

    @property
    def full_name(self) -> str:
        """The full name, e.g., ``uavcan.node.Heartbeat``."""
        return self._name

    @property
    def name_components(self) -> typing.List[str]:
        """Components of the full name as a list, e.g., ``['uavcan', 'node', 'Heartbeat']``."""
        return self._name.split(CompositeType.NAME_COMPONENT_SEPARATOR)

    @property
    def short_name(self) -> str:
        """The last component of the full name, e.g., ``Heartbeat`` of ``uavcan.node.Heartbeat``."""
        return self.name_components[-1]

    @property
    def full_namespace(self) -> str:
        """The full name without the short name, e.g., ``uavcan.node`` for ``uavcan.node.Heartbeat``."""
        return str(CompositeType.NAME_COMPONENT_SEPARATOR.join(self.name_components[:-1]))

    @property
    def root_namespace(self) -> str:
        """The first component of the full name, e.g., ``uavcan`` of ``uavcan.node.Heartbeat``."""
        return self.name_components[0]

    @property
    def version(self) -> Version:
        """The version numbers of the type, e.g., ``(1, 0)`` of ``uavcan.node.Heartbeat.1.0``."""
        return self._version

    @property
    def final(self) -> bool:
        """
        Whether the definition is marked ``@final``. Finality implies that the delimiter header is not used.
        """
        return self._final

    @property
    def extent(self) -> int:
        """
        The amount of memory, in bits, that needs to be allocated in order to store a serialized representation of
        this type or any of its minor versions under the same major version.
        This value is always at least as large as the sum of maximum bit lengths of all fields padded to one byte.

        If the type is :attr:`final`, its extent is at the minimum padded to :attr:`alignment_requirement`.
        If the type is not :attr:`final`, its extent can be specified explicitly via ``@extent`` (provided that it
        is not less than the minimum); otherwise, it defaults to the double of the sum of maximum bit lengths
        of all fields padded to :attr:`alignment_requirement` (which is not the same as twice the minimum).
        """
        return self._extent

    @cached_property
    def bit_length_set(self) -> BitLengthSet:
        """
        The bit length set of a composite is always aligned at :attr:`alignment_requirement`.

        For a :attr:`final` type this is the true bit length set computed by aggregating the fields and
        padding the result to :attr:`alignment_requirement`.
        That is, final types expose their internal structure; for example, a type that contains a single field
        of type ``uint32[2]`` would have a single entry in the bit length set: ``{64}``.

        For a non-final type, not many guarantees about the bit length set can be provided,
        because the type may be mutated in the next minor revision. Therefore, a synthetic bit length set is
        constructed that is merely a list of all possible bit lengths plus the delimiter header.
        For example, a type that contains a single field of type ``uint32[2]`` would have the bit length set of
        ``{h, h+8, h+16, ..., h+56, h+64}`` where ``h`` is the length of the delimiter header.
        """
        if self.final:
            return self._aggregate_bit_length_sets().pad_to_alignment(self.alignment_requirement)
        else:
            return BitLengthSet(range(self.extent + 1)).pad_to_alignment(self.alignment_requirement) +\
                self.delimiter_header_type.bit_length

    @property
    def delimiter_header_type(self) -> typing.Optional[UnsignedIntegerType]:
        """
        If the type is :attr:`final`, the delimiter header is not used and this property is None.
        Otherwise, the delimiter header is an unsigned integer type of a standard bit length.

        Non-final composites are serialized into opaque containers like ``uint8[<=(bit_length_set + 7) // 8]``,
        where the implicit length prefix is of this type.
        Their bit length set is also computed as if it was an array as declared above,
        in order to prevent the containing definitions from making assumptions about the offsets of the following fields
        that might not survive the evolution of the type (e.g., version 1 may be 64 bits long, version 2 might be
        56 bits long).
        """
        if not self.final:
            bit_length = self.DEFAULT_DELIMITER_HEADER_BIT_LENGTH  # This may be made configurable later.
            # This is to prevent the delimiter header from breaking the alignment of the following composite.
            bit_length = max(bit_length, self.alignment_requirement)
            return UnsignedIntegerType(bit_length, UnsignedIntegerType.CastMode.SATURATED)
        return None

    @property
    def deprecated(self) -> bool:
        """Whether the definition is marked ``@deprecated``."""
        return self._deprecated

    @property
    def attributes(self) -> typing.List[Attribute]:
        return self._attributes[:]  # Return copy to prevent mutation

    @property
    def fields(self) -> typing.List[Field]:
        return [a for a in self.attributes if isinstance(a, Field)]

    @property
    def fields_except_padding(self) -> typing.List[Field]:
        return [a for a in self.attributes if isinstance(a, Field) and not isinstance(a, PaddingField)]

    @property
    def constants(self) -> typing.List[Constant]:
        return [a for a in self.attributes if isinstance(a, Constant)]

    @property
    def fixed_port_id(self) -> typing.Optional[int]:
        return self._fixed_port_id

    @property
    def has_fixed_port_id(self) -> bool:
        return self.fixed_port_id is not None

    @property
    def source_file_path(self) -> str:
        """
        For synthesized types such as service request/response sections, this property is defined as an empty string.
        """
        return self._source_file_path

    @property
    def alignment_requirement(self) -> int:
        # This is more general than required by the Specification, but it is done this way in case if we decided
        # to support greater alignment requirements in the future.
        return max(self.BITS_PER_BYTE, *(x.data_type.alignment_requirement for x in self.fields))

    @property
    def parent_service(self) -> typing.Optional['ServiceType']:
        """
        :class:`pydsdl.ServiceType` contains two special fields of this type: ``request`` and ``response``.
        For them this property points to the parent service instance; otherwise it's None.
        """
        return self._parent_service

    @abc.abstractmethod
    def iterate_fields_with_offsets(self, base_offset: typing.Optional[BitLengthSet] = None) \
            -> typing.Iterator[typing.Tuple[Field, BitLengthSet]]:
        """
        Iterates over every field (not attribute -- constants are excluded) of the data type,
        yielding it together with its offset, where the offset is represented as :class:`pydsdl.BitLengthSet`.
        The offset of each field is added to the base offset, which may be specified by the caller;
        if not specified, the base offset is assumed to be ``{0}``.

        The objective of this method is to allow code generators to easily implement fully unrolled serialization and
        deserialization routines, where "unrolled" means that upon encountering another (nested) composite type, the
        serialization routine would not delegate its serialization to the serialization routine of the encountered type,
        but instead would serialize it in-place, as if the field of that type was replaced with its own fields in-place.
        The lack of delegation has very important performance implications: when the serialization routine does
        not delegate serialization of the nested types, it can perform infinitely deep field alignment analysis,
        thus being able to reliably statically determine whether each field of the type, including nested types
        at arbitrarily deep levels of nesting, is aligned relative to the origin of the serialized representation
        of the outermost type. As a result, the code generator will be able to avoid unnecessary reliance on slow
        bit-level copy routines replacing them instead with much faster byte-level copy (like ``memcpy()``) or even
        plain memory aliasing.

        When invoked on a tagged union type, the method yields the same offset for every field (since that's how
        tagged unions are serialized), where the offset equals the bit length of the implicit union tag (plus the
        base offset, of course, if provided).

        Please refer to the usage examples to see how this feature can be used.

        :param base_offset: Assume the specified base offset; assume zero offset if the parameter is not provided.
            The base offset will be implicitly padded out to :attr:`alignment_requirement`.

        :return: A generator of ``(Field, BitLengthSet)``.
            Each instance of :class:`pydsdl.BitLengthSet` yielded by the generator is a dedicated copy,
            meaning that the consumer can mutate the returned instances arbitrarily without affecting future values.
            It is guaranteed that each yielded instance is non-empty.
        """
        raise NotImplementedError

    def _attribute(self, name: _expression.String) -> _expression.Any:
        """This is the handler for DSDL expressions like ``uavcan.node.Heartbeat.1.0.MODE_OPERATIONAL``."""
        for c in self.constants:
            if c.name == name.native_value:
                assert isinstance(c.value, _expression.Any)
                return c.value

        if name.native_value == '_extent_':  # Experimental non-standard extension
            return _expression.Rational(self.extent)

        return super(CompositeType, self)._attribute(name)  # Hand over up the inheritance chain, this is important

    @abc.abstractmethod
    def _aggregate_bit_length_sets(self) -> BitLengthSet:
        """
        Compute the aggregate bit length set for the current composite disregarding the outer padding.
        The aggregation policy is dependent on the kind of the type: either union or struct.
        """
        raise NotImplementedError

    def __getitem__(self, attribute_name: str) -> Attribute:
        """
        Allows the caller to retrieve an attribute by name.
        Padding fields are not accessible via this interface because they don't have names.
        Raises :class:`KeyError` if there is no such attribute.
        """
        return self._attributes_by_name[attribute_name]

    def __str__(self) -> str:
        """Returns a string like ``uavcan.node.Heartbeat.1.0``."""
        return '%s.%d.%d' % (self.full_name, self.version.major, self.version.minor)

    def __repr__(self) -> str:
        return (
            '%s(name=%r, version=%r, fields=%r, constants=%r, alignment_requirement=%r, extent=%r, final=%r, '
            'deprecated=%r, fixed_port_id=%r)'
        ) % (
            self.__class__.__name__,
            self.full_name,
            self.version,
            self.fields,
            self.constants,
            self.alignment_requirement,
            self.extent,
            self.final,
            self.deprecated,
            self.fixed_port_id,
        )


class UnionType(CompositeType):
    """
    A message type that is marked ``@union``.
    """

    MIN_NUMBER_OF_VARIANTS = 2

    def __init__(self,
                 name:             str,
                 version:          Version,
                 attributes:       typing.Iterable[Attribute],
                 extent:           typing.Optional[int],
                 final:            bool,
                 deprecated:       bool,
                 fixed_port_id:    typing.Optional[int],
                 source_file_path: str,
                 parent_service:   typing.Optional['ServiceType'] = None):
        # Proxy all parameters directly to the base type - I wish we could do that
        # with kwargs while preserving the type information
        super(UnionType, self).__init__(name=name,
                                        version=version,
                                        attributes=attributes,
                                        extent=extent,
                                        final=final,
                                        deprecated=deprecated,
                                        fixed_port_id=fixed_port_id,
                                        source_file_path=source_file_path,
                                        parent_service=parent_service)

        if self.number_of_variants < self.MIN_NUMBER_OF_VARIANTS:
            raise MalformedUnionError('A tagged union cannot contain fewer than %d variants' %
                                      self.MIN_NUMBER_OF_VARIANTS)

        for a in attributes:
            if isinstance(a, PaddingField) or not a.name or isinstance(a.data_type, VoidType):
                raise MalformedUnionError('Padding fields not allowed in unions')

        self._tag_field_type = UnsignedIntegerType(self._compute_tag_bit_length([x.data_type for x in self.fields]),
                                                   PrimitiveType.CastMode.TRUNCATED)

    @property
    def number_of_variants(self) -> int:
        return len(self.fields)

    @property
    def tag_field_type(self) -> UnsignedIntegerType:
        """
        The unsigned integer type of the implicit union tag field.
        Note that the set of valid tag values is a subset of that of the returned type.
        """
        return self._tag_field_type

    def iterate_fields_with_offsets(self, base_offset: typing.Optional[BitLengthSet] = None) \
            -> typing.Iterator[typing.Tuple[Field, BitLengthSet]]:
        """See the base class."""
        base_offset = BitLengthSet(base_offset or {0}).pad_to_alignment(self.alignment_requirement)
        base_offset += self.tag_field_type.bit_length
        for f in self.fields:  # Same offset for every field, because it's a tagged union, not a struct
            assert base_offset.is_aligned_at(f.data_type.alignment_requirement)
            yield f, BitLengthSet(base_offset)      # We yield a copy of the offset to prevent mutation

    def _aggregate_bit_length_sets(self) -> BitLengthSet:
        return self.aggregate_bit_length_sets([x.data_type for x in self.fields])

    @staticmethod
    def aggregate_bit_length_sets(field_types: typing.Sequence[SerializableType]) -> BitLengthSet:
        """
        Computes the bit length set for a tagged union type given the type of each of its variants.
        The final padding is not applied.

        Unions are easy to handle because when serialized, a union is essentially just a single field prefixed with
        a fixed-length integer tag. So we just build a full set of combinations and then add the tag length
        to each element.

        Observe that unions are not defined for less than 2 elements;
        however, this function tries to be generic by properly handling those cases as well,
        even though they are not permitted by the specification.
        For zero fields, the function yields ``{0}``; for one field, the function yields the BLS of the field itself.
        """
        ms = [x.bit_length_set for x in field_types]
        if len(ms) == 0:
            return BitLengthSet(0)
        if len(ms) == 1:
            return BitLengthSet(ms[0])

        out = BitLengthSet()
        for s in ms:
            out |= s
        out += UnionType._compute_tag_bit_length(field_types)  # Add the union tag
        return out

    @staticmethod
    def _compute_tag_bit_length(field_types: typing.Sequence[SerializableType]) -> int:
        if len(field_types) > 1:
            unaligned_tag_bit_length = (len(field_types) - 1).bit_length()
            tag_bit_length = 2 ** math.ceil(math.log2(max(SerializableType.BITS_PER_BYTE, unaligned_tag_bit_length)))
            # This is to prevent the tag from breaking the alignment of the following variant.
            tag_bit_length = max([tag_bit_length] + [x.alignment_requirement for x in field_types])
            assert tag_bit_length in {8, 16, 32, 64}
            return tag_bit_length
        else:
            raise ValueError('Invalid number of variants: ' + repr(len(field_types)))


class StructureType(CompositeType):
    """
    A message type that is NOT marked ``@union``.
    """

    def iterate_fields_with_offsets(self, base_offset: typing.Optional[BitLengthSet] = None) \
            -> typing.Iterator[typing.Tuple[Field, BitLengthSet]]:
        """See the base class."""
        base_offset = BitLengthSet(base_offset or 0).pad_to_alignment(self.alignment_requirement)
        for f in self.fields:
            base_offset.pad_to_alignment(f.data_type.alignment_requirement)
            yield f, BitLengthSet(base_offset)      # We yield a copy of the offset to prevent mutation
            base_offset += f.data_type.bit_length_set

    def _aggregate_bit_length_sets(self) -> BitLengthSet:
        return self.aggregate_bit_length_sets([f.data_type for f in self.fields])

    @staticmethod
    def aggregate_bit_length_sets(field_types: typing.Sequence[SerializableType]) -> BitLengthSet:
        """
        Computes the bit length set for a structure type given the type of each of its fields.
        The final padding is not applied.
        """
        bls = BitLengthSet()
        for t in field_types:
            bls.pad_to_alignment(t.alignment_requirement)
            bls += t.bit_length_set
        return bls


class ServiceType(CompositeType):
    """
    A service (not message) type.
    Unlike message types, it can't be serialized directly.

    There are exactly two pseudo-fields: ``request`` and ``response``,
    which contain the request and the response structure of the service type, respectively.
    """

    class SchemaParams:
        """A trivial helper dataclass used for constructing new instances."""
        def __init__(self,
                     attributes: typing.Iterable[Attribute],
                     extent:     typing.Optional[int],
                     is_union:   bool,
                     is_final:   bool):
            self.attributes = list(attributes)
            self.extent = int(extent) if extent is not None else None
            self.is_union = bool(is_union)
            self.is_final = bool(is_final)

    def __init__(self,
                 name:             str,
                 version:          Version,
                 request_params:   SchemaParams,
                 response_params:  SchemaParams,
                 deprecated:       bool,
                 fixed_port_id:    typing.Optional[int],
                 source_file_path: str):
        request_meta_type = UnionType if request_params.is_union else StructureType  # type: type
        self._request_type = request_meta_type(name=name + '.Request',
                                               version=version,
                                               attributes=request_params.attributes,
                                               extent=request_params.extent,
                                               final=request_params.is_final,
                                               deprecated=deprecated,
                                               fixed_port_id=None,
                                               source_file_path='',
                                               parent_service=self)  # type: CompositeType

        response_meta_type = UnionType if response_params.is_union else StructureType  # type: type
        self._response_type = response_meta_type(name=name + '.Response',
                                                 version=version,
                                                 attributes=response_params.attributes,
                                                 extent=response_params.extent,
                                                 final=response_params.is_final,
                                                 deprecated=deprecated,
                                                 fixed_port_id=None,
                                                 source_file_path='',
                                                 parent_service=self)  # type: CompositeType

        container_attributes = [
            Field(data_type=self._request_type,  name='request'),
            Field(data_type=self._response_type, name='response'),
        ]
        super(ServiceType, self).__init__(
            name=name,
            version=version,
            attributes=container_attributes,
            extent=None,
            final=True,
            deprecated=deprecated,
            fixed_port_id=fixed_port_id,
            source_file_path=source_file_path,
        )
        assert self.request_type.parent_service is self
        assert self.response_type.parent_service is self

    @property
    def request_type(self) -> CompositeType:
        """The type of the request schema."""
        return self._request_type

    @property
    def response_type(self) -> CompositeType:
        """The type of the response schema."""
        return self._response_type

    def iterate_fields_with_offsets(self, base_offset: typing.Optional[BitLengthSet] = None) \
            -> typing.Iterator[typing.Tuple[Field, BitLengthSet]]:
        """Always raises a :class:`TypeError`."""
        raise TypeError('Service types do not have serializable fields. Use either request or response.')

    def _aggregate_bit_length_sets(self) -> BitLengthSet:  # pragma: no cover
        raise TypeError('Service types are not directly serializable. Use either request or response.')


def _unittest_composite_types() -> None:
    from pytest import raises
    from ._primitive import SignedIntegerType, FloatType
    from ._array import FixedLengthArrayType, VariableLengthArrayType

    def try_name(name: str) -> CompositeType:
        return StructureType(name=name,
                             version=Version(0, 1),
                             attributes=[],
                             deprecated=False,
                             fixed_port_id=None,
                             source_file_path='')

    with raises(InvalidNameError, match='(?i).*empty.*'):
        try_name('')

    with raises(InvalidNameError, match='(?i).*root namespace.*'):
        try_name('T')

    with raises(InvalidNameError, match='(?i).*long.*'):
        try_name('namespace.another.deeper.' * 10 + 'LongTypeName')

    with raises(InvalidNameError, match='(?i).*component.*empty.*'):
        try_name('namespace.ns..T')

    with raises(InvalidNameError, match='(?i).*component.*empty.*'):
        try_name('.namespace.ns.T')

    with raises(InvalidNameError, match='(?i).*cannot start with.*'):
        try_name('namespace.0ns.T')

    with raises(InvalidNameError, match='(?i).*cannot start with.*'):
        try_name('namespace.ns.0T')

    with raises(InvalidNameError, match='(?i).*cannot contain.*'):
        try_name('namespace.n-s.T')

    assert try_name('root.nested.T').full_name == 'root.nested.T'
    assert try_name('root.nested.T').full_namespace == 'root.nested'
    assert try_name('root.nested.T').root_namespace == 'root'
    assert try_name('root.nested.T').short_name == 'T'

    print(ServiceType(name='a' * 48 + '.T',     # No exception raised
                      version=Version(0, 1),
                      request_attributes=[],
                      response_attributes=[],
                      request_is_union=False,
                      response_is_union=False,
                      deprecated=False,
                      fixed_port_id=None,
                      source_file_path=''))

    with raises(MalformedUnionError, match='.*variants.*'):
        UnionType(name='a.A',
                  version=Version(0, 1),
                  attributes=[],
                  deprecated=False,
                  fixed_port_id=None,
                  source_file_path='')

    with raises(MalformedUnionError, match='(?i).*padding.*'):
        UnionType(name='a.A',
                  version=Version(0, 1),
                  attributes=[
                      Field(UnsignedIntegerType(16, PrimitiveType.CastMode.TRUNCATED), 'a'),
                      Field(SignedIntegerType(16, PrimitiveType.CastMode.SATURATED), 'b'),
                      PaddingField(VoidType(16)),
                  ],
                  deprecated=False,
                  fixed_port_id=None,
                  source_file_path='')

    u = UnionType(name='uavcan.node.Heartbeat',
                  version=Version(42, 123),
                  attributes=[
                      Field(UnsignedIntegerType(16, PrimitiveType.CastMode.TRUNCATED), 'a'),
                      Field(SignedIntegerType(16, PrimitiveType.CastMode.SATURATED), 'b'),
                      Constant(FloatType(32, PrimitiveType.CastMode.SATURATED), 'A', _expression.Rational(123)),
                  ],
                  deprecated=False,
                  fixed_port_id=None,
                  source_file_path='')
    assert u['a'].name == 'a'
    assert u['b'].name == 'b'
    assert u['A'].name == 'A'
    assert u.fields == u.fields_except_padding
    with raises(KeyError):
        assert u['c']
    assert hash(u) == hash(u)
    del u

    s = StructureType(name='a.A',
                      version=Version(0, 1),
                      attributes=[
                          PaddingField(VoidType(8)),
                          Field(UnsignedIntegerType(16, PrimitiveType.CastMode.TRUNCATED), 'a'),
                          PaddingField(VoidType(64)),
                          Field(SignedIntegerType(16, PrimitiveType.CastMode.SATURATED), 'b'),
                          PaddingField(VoidType(2)),
                          Constant(FloatType(32, PrimitiveType.CastMode.SATURATED), 'A', _expression.Rational(123)),
                      ],
                      deprecated=False,
                      fixed_port_id=None,
                      source_file_path='')
    assert s['a'].name == 'a'
    assert s['b'].name == 'b'
    assert s['A'].name == 'A'
    assert len(s.constants) == 1
    assert len(s.fields) == 5
    assert len(s.fields_except_padding) == 2
    with raises(KeyError):
        assert s['c']
    with raises(KeyError):
        assert s['']        # Padding fields are not accessible
    assert hash(s) == hash(s)
    del s

    def try_union_fields(field_types: typing.List[SerializableType]) -> UnionType:
        atr = []
        for i, t in enumerate(field_types):
            atr.append(Field(t, '_%d' % i))

        return UnionType(name='a.A',
                         version=Version(0, 1),
                         attributes=atr,
                         deprecated=False,
                         fixed_port_id=None,
                         source_file_path='')

    assert try_union_fields([
        UnsignedIntegerType(16, PrimitiveType.CastMode.TRUNCATED),
        SignedIntegerType(16, PrimitiveType.CastMode.SATURATED),
    ]).bit_length_set == {24}

    assert try_union_fields(
        [
            UnsignedIntegerType(16, PrimitiveType.CastMode.TRUNCATED),
            SignedIntegerType(16, PrimitiveType.CastMode.SATURATED),
        ] * 1000
    ).bit_length_set == {16 + 16}

    assert try_union_fields(
        [
            UnsignedIntegerType(16, PrimitiveType.CastMode.TRUNCATED),
            SignedIntegerType(16, PrimitiveType.CastMode.SATURATED),
        ] * 1000000
    ).bit_length_set == {32 + 16}

    # The reference values for the following test are explained in the array tests above
    tu8 = UnsignedIntegerType(8, cast_mode=PrimitiveType.CastMode.TRUNCATED)
    small = VariableLengthArrayType(tu8, 2)
    outer = FixedLengthArrayType(small, 2)   # bit length values: {4, 12, 20, 28, 36}

    # Above plus one bit to each, plus 16-bit for the unsigned integer field
    assert try_union_fields([
        outer,
        SignedIntegerType(16, PrimitiveType.CastMode.SATURATED),
    ]).bit_length_set == {24, 32, 40, 48, 56}

    def try_struct_fields(field_types: typing.List[SerializableType]) -> StructureType:
        atr = []
        for i, t in enumerate(field_types):
            atr.append(Field(t, '_%d' % i))

        return StructureType(name='a.A',
                             version=Version(0, 1),
                             attributes=atr,
                             deprecated=False,
                             fixed_port_id=None,
                             source_file_path='')

    assert try_struct_fields([
        UnsignedIntegerType(16, PrimitiveType.CastMode.TRUNCATED),
        SignedIntegerType(16, PrimitiveType.CastMode.SATURATED),
    ]).bit_length_set == {32}

    assert try_struct_fields([]).bit_length_set == {0}   # Empty sets forbidden

    assert try_struct_fields([
        outer,
        SignedIntegerType(16, PrimitiveType.CastMode.SATURATED),
    ]).bit_length_set == {16 + 16, 24 + 16, 32 + 16, 40 + 16, 48 + 16}

    assert try_struct_fields([outer]).bit_length_set == {16, 24, 32, 40, 48}


def _unittest_field_iterators() -> None:
    from pytest import raises
    from ._primitive import BooleanType, FloatType
    from ._array import FixedLengthArrayType, VariableLengthArrayType

    saturated = PrimitiveType.CastMode.SATURATED
    _seq_no = 0

    def make_type(meta: typing.Type[CompositeType], attributes: typing.Iterable[Attribute]) -> CompositeType:
        nonlocal _seq_no
        _seq_no += 1
        return meta('ns.Type' + str(_seq_no),
                    version=Version(1, 0),
                    attributes=attributes,
                    deprecated=False,
                    fixed_port_id=None,
                    source_file_path='')

    def validate_iterator(t: CompositeType,
                          reference: typing.Iterable[typing.Tuple[str, typing.Set[int]]],
                          base_offset: typing.Optional[BitLengthSet] = None) -> None:
        for (name, ref_set), (field, real_set) in itertools.zip_longest(reference,
                                                                        t.iterate_fields_with_offsets(base_offset)):
            assert field.name == name
            assert real_set == ref_set, field.name + ': ' + str(real_set)

    a = make_type(StructureType, [
        Field(UnsignedIntegerType(10, saturated), 'a'),
        Field(BooleanType(saturated), 'b'),
        Field(VariableLengthArrayType(FloatType(32, saturated), 2), 'c'),
        Field(FixedLengthArrayType(FloatType(32, saturated), 7), 'd'),
        PaddingField(VoidType(3)),
    ])

    validate_iterator(a, [
        ('a', {0}),
        ('b', {10}),
        ('c', {11}),
        ('d', {
            11 + 8 + 32 * 0,
            11 + 8 + 32 * 1,
            11 + 8 + 32 * 2,
        }),
        ('', {
            11 + 8 + 32 * 0 + 32 * 7,
            11 + 8 + 32 * 1 + 32 * 7,
            11 + 8 + 32 * 2 + 32 * 7,
        }),
    ])

    a_bls_options = [
        11 + 8 + 32 * 0 + 32 * 7 + 3,
        11 + 8 + 32 * 1 + 32 * 7 + 3,
        11 + 8 + 32 * 2 + 32 * 7 + 3,
    ]
    assert a.bit_length_set == BitLengthSet(a_bls_options)

    # Testing "a" again, this time with non-zero base offset
    validate_iterator(a, [
        ('a', {1, 16}),
        ('b', {1 + 10, 16 + 10}),
        ('c', {1 + 11, 16 + 11}),
        ('d', {
            1 + 11 + 8 + 32 * 0,
            1 + 11 + 8 + 32 * 1,
            1 + 11 + 8 + 32 * 2,
            16 + 11 + 8 + 32 * 0,
            16 + 11 + 8 + 32 * 1,
            16 + 11 + 8 + 32 * 2,
        }),
        ('', {
            1 + 11 + 8 + 32 * 0 + 32 * 7,
            1 + 11 + 8 + 32 * 1 + 32 * 7,
            1 + 11 + 8 + 32 * 2 + 32 * 7,
            16 + 11 + 8 + 32 * 0 + 32 * 7,
            16 + 11 + 8 + 32 * 1 + 32 * 7,
            16 + 11 + 8 + 32 * 2 + 32 * 7,
        }),
    ], BitLengthSet({1, 16}))

    b = make_type(StructureType, [
        Field(a, 'z'),
        Field(VariableLengthArrayType(a, 2), 'y'),
        Field(UnsignedIntegerType(6, saturated), 'x'),
    ])

    validate_iterator(b, [
        ('z', {0}),
        ('y', {
            a_bls_options[0],
            a_bls_options[1],
            a_bls_options[2],
        }),
        ('x', {  # The lone "+2" is for the variable-length array's implicit length field
            # First length option of z
            a_bls_options[0] + 8 + a_bls_options[0] * 0,  # suka
            a_bls_options[0] + 8 + a_bls_options[1] * 0,
            a_bls_options[0] + 8 + a_bls_options[2] * 0,
            a_bls_options[0] + 8 + a_bls_options[0] * 1,
            a_bls_options[0] + 8 + a_bls_options[1] * 1,
            a_bls_options[0] + 8 + a_bls_options[2] * 1,
            a_bls_options[0] + 8 + a_bls_options[0] * 2,
            a_bls_options[0] + 8 + a_bls_options[1] * 2,
            a_bls_options[0] + 8 + a_bls_options[2] * 2,
            # Second length option of z
            a_bls_options[1] + 8 + a_bls_options[0] * 0,
            a_bls_options[1] + 8 + a_bls_options[1] * 0,
            a_bls_options[1] + 8 + a_bls_options[2] * 0,
            a_bls_options[1] + 8 + a_bls_options[0] * 1,
            a_bls_options[1] + 8 + a_bls_options[1] * 1,
            a_bls_options[1] + 8 + a_bls_options[2] * 1,
            a_bls_options[1] + 8 + a_bls_options[0] * 2,
            a_bls_options[1] + 8 + a_bls_options[1] * 2,
            a_bls_options[1] + 8 + a_bls_options[2] * 2,
            # Third length option of z
            a_bls_options[2] + 8 + a_bls_options[0] * 0,
            a_bls_options[2] + 8 + a_bls_options[1] * 0,
            a_bls_options[2] + 8 + a_bls_options[2] * 0,
            a_bls_options[2] + 8 + a_bls_options[0] * 1,
            a_bls_options[2] + 8 + a_bls_options[1] * 1,
            a_bls_options[2] + 8 + a_bls_options[2] * 1,
            a_bls_options[2] + 8 + a_bls_options[0] * 2,
            a_bls_options[2] + 8 + a_bls_options[1] * 2,
            a_bls_options[2] + 8 + a_bls_options[2] * 2,
        }),
    ])

    # Ensuring the equivalency between bit length and bit offset
    b_offset = BitLengthSet()
    for f in b.fields:
        b_offset += f.data_type.bit_length_set
    print('b_offset:', b_offset)
    assert b_offset == b.bit_length_set
    assert not b_offset.is_aligned_at_byte()
    assert not b_offset.is_aligned_at(32)

    c = make_type(UnionType, [
        Field(a, 'foo'),
        Field(b, 'bar'),
    ])

    validate_iterator(c, [
        ('foo', {8}),       # The offset is the same because it's a union
        ('bar', {8}),
    ])

    validate_iterator(c, [
        ('foo', {8 + 8}),
        ('bar', {8 + 8}),
    ], BitLengthSet(8))

    validate_iterator(c, [
        ('foo', {0 + 8, 4 + 8, 8 + 8}),
        ('bar', {0 + 8, 4 + 8, 8 + 8}),
    ], BitLengthSet({0, 4, 8}))

    with raises(TypeError, match='.*request or response.*'):
        ServiceType(name='ns.S',
                    version=Version(1, 0),
                    request_attributes=[],
                    response_attributes=[],
                    request_is_union=False,
                    response_is_union=False,
                    deprecated=False,
                    fixed_port_id=None,
                    source_file_path='').iterate_fields_with_offsets()
