# -*- coding: utf-8 -*-
# *********************************************************************
# lewis - a library for creating hardware device simulators
# Copyright (C) 2016 European Spallation Source ERIC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
# *********************************************************************

from __future__ import print_function

import asynchat
import asyncore
import inspect
import re
import socket
from argparse import ArgumentParser

from six import b

from lewis.adapters import Adapter
from lewis.core.utils import format_doc_text


class StreamHandler(asynchat.async_chat):
    def __init__(self, sock, target):
        asynchat.async_chat.__init__(self, sock=sock)
        self.set_terminator(b(target.in_terminator))
        self.target = target
        self.buffer = []

    def collect_incoming_data(self, data):
        self.buffer.append(data)

    def found_terminator(self):
        request = b''.join(self.buffer)
        reply = None
        self.buffer = []

        try:
            cmd = next((cmd for cmd in self.target.bound_commands if cmd.can_process(request)),
                       None)

            if cmd is None:
                raise RuntimeError('None of the device\'s commands matched.')

            reply = cmd.process_request(request)

        except Exception as error:
            reply = self.target.handle_error(request, error)

        if reply is not None:
            self.push(b(reply + self.target.out_terminator))


class StreamServer(asyncore.dispatcher):
    def __init__(self, host, port, target):
        asyncore.dispatcher.__init__(self)
        self.target = target
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind((host, port))
        self.listen(5)

    def handle_accept(self):
        pair = self.accept()
        if pair is not None:
            sock, addr = pair
            print("Client connect from %s" % repr(addr))
            StreamHandler(sock, self.target)


class CommandBase(object):
    def __init__(self, member, argument_mappings=None, return_mapping=None, doc=None):
        self.member = member
        self.argument_mappings = argument_mappings
        self.return_mapping = return_mapping
        self.doc = doc

    def bind(self, target):
        raise NotImplementedError('Binders need to implement the bind method.')


class Cmd(CommandBase):
    """
    This class is used to define commands in terms of a string pattern that are connected to
    a method of the device object owned by :class:`StreamAdapter`.

    Method arguments are indicated by groups in the regular expression. The number of
    groups has to match the number of arguments of the method. The optional argument_mappings
    can be an iterable of callables with one parameter of the same length as the
    number of arguments of the method. The first parameter will be transformed using the
    first function, the second using the second function and so on. This can be useful
    to automatically transform strings provided by the adapter into a proper data type
    such as ``int`` or ``float`` before they are passed to the method.

    The return_mapping argument is similar, it should map the return value of the method
    to a string. The default map function only does that when the supplied value
    is not None. It can also be set to a numeric value or a string constant so that the
    command always returns the same value. If it is ``None``, the return value is not
    modified at all.

    Finally, documentation can be provided by passing the doc-argument. If it is omitted,
    the docstring of the bound method is used and if that is not present, left empty.

    .. seealso ::

        :class:`Var` exposes attributes and properties of a device object.

    :param target_method: Method to be called when regex matches.
    :param pattern: Regex to match for method call.
    :param argument_mappings: Iterable with mapping functions from string to some type.
    :param return_mapping: Mapping function for return value of method.
    :param doc: Description of the command. If not supplied, the docstring is used.
    """

    func = None

    def __init__(self, func, pattern, argument_mappings=None,
                 return_mapping=lambda x: None if x is None else str(x), doc=None):
        super(Cmd, self).__init__(None, argument_mappings, return_mapping,
                                  doc or inspect.getdoc(self.func))

        self.func = func
        self.raw_pattern = pattern
        self.pattern = re.compile(b(pattern), 0) if pattern else None

        if argument_mappings is not None and (self.pattern.groups != len(argument_mappings)):
            raise RuntimeError(
                'Expected {} argument mapping(s), got {}'.format(
                    self.pattern.groups, len(argument_mappings)))

        self.argument_mappings = argument_mappings
        self.return_mapping = return_mapping
        self.doc = doc or (inspect.getdoc(self.func) if callable(self.func) else None)

    def bind(self, target):
        if callable(self.func):
            return [self]

        method = getattr(target, self.func, None)

        if method is None:
            return None

        return [Cmd(method, self.raw_pattern, self.argument_mappings, self.return_mapping,
                    self.doc)]

    def can_process(self, request):
        return self.pattern.match(request) is not None

    def process_request(self, request):
        if not callable(self.func):
            raise RuntimeError('Trying to process request on unbound Binder.')

        match = self.pattern.match(request)

        if not match:
            raise RuntimeError('Request can not be processed.')

        args = self.map_arguments(match.groups())

        return self.map_return_value(self.func(*args))

    def map_arguments(self, arguments):
        """
        Returns the mapped function arguments. If no mapping functions are defined, the arguments
        are returned as they were supplied.

        :param arguments: List of arguments for bound function as strings.
        :return: Mapped arguments.
        """
        if self.argument_mappings is None:
            return arguments

        return [f(a) for f, a in zip(self.argument_mappings, arguments)]

    def map_return_value(self, return_value):
        """
        Returns the mapped return_value of a processed request. If no return_mapping has been
        defined, the value is returned as is.

        :param return_value: Value to map.
        :return: Mapped return value.
        """
        if callable(self.return_mapping):
            return self.return_mapping(return_value)

        if self.return_mapping is not None:
            return self.return_mapping

        return return_value


class Var(CommandBase):
    """
    With this class it's possible to define read and
    """

    def __init__(self, target_member, read_pattern=None, write_pattern=None,
                 argument_mappings=None, return_mapping=lambda x: None if x is None else str(x),
                 doc=None):
        super(Var, self).__init__(target_member, argument_mappings, return_mapping, doc)

        self.member = target_member
        self.target = None

        self.read_pattern = read_pattern
        self.write_pattern = write_pattern

    def bind(self, target):
        if self.member not in dir(target):
            return None

        funcs = []

        if self.read_pattern is not None:
            def getter():
                return getattr(target, self.member)

            if inspect.isdatadescriptor(getattr(type(target), self.member)):
                getter.__doc__ = 'Getter: ' + inspect.getdoc(getattr(type(target), self.member))

            funcs.append(
                Cmd(getter, self.read_pattern, return_mapping=self.return_mapping, doc=self.doc))

        if self.write_pattern is not None:
            def setter(new_value):
                setattr(target, self.member, new_value)

            if inspect.isdatadescriptor(getattr(type(target), self.member)):
                setter.__doc__ = 'Setter: ' + inspect.getdoc(getattr(type(target), self.member))

            funcs.append(
                Cmd(setter, self.write_pattern, argument_mappings=self.argument_mappings,
                    return_mapping=self.return_mapping, doc=self.doc))

        return funcs


class StreamAdapter(Adapter):
    """
    This class is used to provide a TCP-stream based interface to a device.

    Many hardware devices use a protocol that is based on exchanging text with a client via
    a TCP stream. Sometimes RS232-based devices are also exposed this way via an adapter-box.
    This adapter makes it easy to mimic such a protocol, in a subclass only three members must
    be overridden:

     - in_terminator, out_terminator: These define how lines are terminated when transferred
       to and from the device respectively. They are stripped/added automatically.
       The default is ``\\r``.
     - commands: A list of :class:`~CommandBase`-objects that define mappings between protocol
       and device/interface methods/attributes.

    Commands are expressed as regular expressions, a simple example may look like this:

    .. sourcecode:: Python

        class SimpleDeviceStreamInterface(StreamAdapter):
            commands = [
                Cmd('set_speed', r'^S=([0-9]+)$', argument_mappings=[int]),
                Cmd('get_speed', r'^S\\?$')
                Var('speed', read_pattern=r'^V\\?$', write_pattern=r'^V=([0-9]+)$')
            ]

            def set_speed(self, new_speed):
                self._device.speed = new_speed

            def get_speed(self):
                return self._device.speed

    The interface has two commands, ``S?`` to return the speed and ``S=10`` to set the speed
    to an integer value.

    As in the :class:`lewis.adapters.epics.EpicsAdapter`, it does not matter whether the
    wrapped method is a part of the device or of the interface, this is handled automatically.

    In addition, the :meth:`handle_error`-method can be overridden. It is called when an exception
    is raised while handling commands.

    :param device: The exposed device.
    :param arguments: Command line arguments.
    """
    protocol = 'stream'

    in_terminator = '\r'
    out_terminator = '\r'

    commands = None

    def __init__(self, device, arguments=None):
        super(StreamAdapter, self).__init__(device, arguments)

        if arguments is not None:
            self._options = self._parseArguments(arguments)

        self._server = None

        self.bound_commands = self._bind_commands(self.commands)

    @property
    def documentation(self):

        commands = ['{}:\n{}'.format(
            cmd.raw_pattern,
            format_doc_text(cmd.doc or inspect.getdoc(cmd.func) or ''))
                    for cmd in self.bound_commands]

        options = format_doc_text(
            'Listening on: {}\nPort: {}\nRequest terminator: {}\nReply terminator: {}'.format(
                self._options.bind_address, self._options.port,
                repr(self.in_terminator), repr(self.out_terminator)))

        return '\n\n'.join(
            [inspect.getdoc(self) or '',
             'Parameters\n==========', options, 'Commands\n========'] + commands)

    def start_server(self):
        """
        Starts the TCP stream server, binding to the configured host and port.
        Host and port are configured via the command line arguments.

        .. note:: The server does not process requests unless
                  :meth:`handle` is called in regular intervals.

        """
        self._server = StreamServer(self._options.bind_address, self._options.port, self)

    def _parseArguments(self, arguments):
        parser = ArgumentParser(description='Adapter to expose a device via TCP Stream')
        parser.add_argument('-b', '--bind-address', default='0.0.0.0',
                            help='IP Address to bind and listen for connections on')
        parser.add_argument('-p', '--port', type=int, default=9999,
                            help='Port to listen for connections on')
        return parser.parse_args(arguments)

    def _bind_commands(self, cmds):
        patterns = set()

        bound_commands = []

        for cmd in cmds:
            bound = cmd.bind(self) or cmd.bind(self._device) or None

            if bound is None:
                raise RuntimeError(
                    'Unable to produce callable object for non-existing member \'{}\' '
                    'of device or interface.'.format(cmd.member))

            for bound_cmd in bound:
                if bound_cmd.pattern in patterns:
                    raise RuntimeError(
                        'The regular expression {} is '
                        'associated with multiple commands.'.format(bound_cmd.pattern.pattern))

                patterns.add(bound_cmd.pattern)

                bound_commands.append(bound_cmd)

        return bound_commands

    def handle_error(self, request, error):
        """
        Override this method to handle exceptions that are raised during command processing.
        The default implementation does nothing, so that any errors are silently ignored.

        :param request: The request that resulted in the error.
        :param error: The exception that was raised.
        """
        pass

    def handle(self, cycle_delay=0.1):
        """
        Spend approximately ``cycle_delay`` seconds to process requests to the server.

        :param cycle_delay: S
        """
        asyncore.loop(cycle_delay, count=1)
