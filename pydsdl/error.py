#
# Copyright (C) 2018  UAVCAN Development Team  <uavcan.org>
# This software is distributed under the terms of the MIT License.
#

import typing
import urllib.parse


class ParseError(Exception):       # PEP8 says that the "Exception" suffix is redundant and should not be used.
    """
    This exception is raised in case if the parser discovers an error in the DSDL code.
    This is the base class for all exceptions raised by the parser's inner logic,
    excepting its entry function which also can raised ValueError when provided with incorrect inputs.
    Fields:
        path    Source file path where the error has occurred. Optional, will be None if unknown.
        line    Source file line number where the error has occurred. Optional, will be None if unknown.
    """

    def __init__(self,
                 text: str,
                 path: typing.Optional[str]=None,
                 line: typing.Optional[int]=None):
        Exception.__init__(self, text)
        self._path = path
        self._line = line

    def set_error_location_if_unknown(self,
                                      path: typing.Optional[str]=None,
                                      line: typing.Optional[int]=None) -> None:
        if not self._path and path:
            self._path = path

        if not self._line and line:
            self._line = line

    @property
    def path(self) -> typing.Optional[str]:
        return self._path

    @property
    def line(self) -> typing.Optional[int]:
        return self._line

    def __str__(self) -> str:
        """Returns a nicely formatted error string in a GCC-like format (can be parsed by e.g. Eclipse error parser)"""
        if self.path and self.line:
            return '%s:%d: %s' % (self.path, self.line, Exception.__str__(self))

        if self.path:
            return '%s: %s' % (self.path, Exception.__str__(self))

        return Exception.__str__(self)

    def __repr__(self) -> str:
        return self.__class__.__name__ + ': ' + repr(self.__str__())


class InternalParserError(ParseError):
    """
    This exception is used to report internal errors in the parser itself that prevented it from
    processing the definitions.
    """
    def __init__(self,
                 text: typing.Optional[str]=None,
                 path: typing.Optional[str]=None,
                 line: typing.Optional[int]=None,
                 culprit: typing.Optional[Exception]=None):
        if culprit is not None:
            report_text = 'PLEASE REPORT AT https://github.com/UAVCAN/pydsdl/issues/new?title=' + \
                          urllib.parse.quote(repr(culprit))
            if text:
                text = text + ' ' + report_text
            else:
                text = report_text

        if not text:
            text = ''

        super(InternalParserError, self).__init__(text=text, path=path, line=line)


class InvalidDefinitionError(ParseError):
    """
    This exception is used to point out mistakes and errors in the parsed definitions.
    """
    pass


class FileNameFormatError(InvalidDefinitionError):
    """
    Raised when a DSDL definition file is named incorrectly.
    """
    def __init__(self, text: str, path: str):
        super(FileNameFormatError, self).__init__(text=text, path=str(path))


class RegulatedPortIDCollisionError(InvalidDefinitionError):
    """
    Raised when there is more than one definition using the same regulated port ID.
    """
    def __init__(self, *, path: str, colliding_paths: typing.Iterable[str]):
        text = 'The regulated port ID of this definition is also used in: %r' % colliding_paths
        super(RegulatedPortIDCollisionError, self).__init__(text=text, path=str(path))


class NamespaceNameCollisionError(InvalidDefinitionError):
    """
    Raised when there is more than one namespace under the same name.
    This may occur if there are identically named namespaces located in different directories.
    """
    def __init__(self, *, path: str, colliding_paths: typing.Iterable[str]):
        text = 'The name of this namespace conflicts with: %r' % colliding_paths
        super(NamespaceNameCollisionError, self).__init__(text=text, path=str(path))


class NestedRootNamespaceError(InvalidDefinitionError):
    """
    Nested root namespaces are not allowed.
    This exception is thrown when this rule is violated.
    """
    def __init__(self, *, outer_path: str, nested_paths: typing.Iterable[str]):
        text = 'The following namespaces are nested inside this one, which is not permitted: %r' % nested_paths
        super(NestedRootNamespaceError, self).__init__(text=text, path=str(outer_path))


def _unittest_exception() -> None:
    try:
        raise ParseError('Hello world!')
    except Exception as ex:
        assert str(ex) == 'Hello world!'
        assert repr(ex) == "ParseError: 'Hello world!'"

    try:
        raise ParseError('Hello world!', path='path/to/file.uavcan', line=123)
    except Exception as ex:
        assert str(ex) == 'path/to/file.uavcan:123: Hello world!'
        assert repr(ex) == "ParseError: 'path/to/file.uavcan:123: Hello world!'"

    try:
        raise ParseError('Hello world!', path='path/to/file.uavcan')
    except Exception as ex:
        assert str(ex) == 'path/to/file.uavcan: Hello world!'
        assert repr(ex) == "ParseError: 'path/to/file.uavcan: Hello world!'"