# -*- coding: utf-8 -*-
#
# Copyright (C) 2012-2013 Vinay M. Sajip. See LICENSE for licensing information.
#
import logging
import os

import sys

from explainshell.shlext import shell_shlex

logger = logging.getLogger(__name__)

# We use a separate logger for parsing, as that's sometimes too much
# information :-)
parse_logger = logging.getLogger('%s.parse' % __name__)
#
# This runs on Python 2.x and 3.x from the same code base - no need for 2to3.
#
if sys.version_info[0] < 3:
    PY3 = False
    text_type = unicode
    binary_type = str
    string_types = basestring,
else:
    PY3 = True
    text_type = str
    binary_type = bytes
    string_types = str,
    basestring = str

#
# A dummy redirects dict which indicates a desire to swap stdout and stderr
# in the child.
#
SWAP_OUTPUTS = {
    1: ('>', ('&', 2)),
    2: ('>', ('&', 3)),
    3: ('>', ('&', 1)),
    }

class Node(object):
    """
    This class represents a node in the AST built while parsing command lines.
    It's basically an object container for various attributes, with a slightly
    specialised representation to make it a little easier to debug the parser.
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        chunks = []
        d = dict(self.__dict__)
        kind = d.pop('kind')
        for k, v in sorted(d.items()):
            chunks.append('%s=%s' % (k, v))
        return '%sNode(%s)' % (kind.title(), ' '.join(chunks))

class CommandLineParser(object):
    """
    This class implements a fairly unsophisticated recursive descent parser for
    shell command lines as used in sh, bash and dash.
    """

    permitted_tokens = ('&&', '||', '|&', '>>')

    def next_token(self):
        t = self.lex.get_token()
        if not t:
            tt = None
        else:
            tt = self.lex.token_type
            if tt in ('"', "'"):
                tt = 'word'
                t = t[1:-1]
            elif tt == 'a':
                try:
                    int(t)
                    tt = 'number'
                except ValueError:
                    tt = 'word'
            elif tt == 'c':
                # the shlex parser will return arbitrary runs of 'control'
                # characters, but only some runs will be valid for us. We
                # split into the valid runs and push all the others back,
                # keeping just one. Since all will have a token_type of
                # 'c', we don't need to worry about pushing the type back.
                if len(t) > 1:
                    valid = self.get_valid_controls(t)
                    t = valid.pop(0)
                    if valid:
                        for other in reversed(valid):
                            self.lex.push_token(other)
                tt = t
        return tt, t, self.lex.preceding

    def get_valid_controls(self, t):
        if len(t) == 1:
            result = [t]
        else:
            result = []
            last = None
            for c in t:
                if last is not None:
                    combined = last + c
                    if combined in self.permitted_tokens:
                        result.append(combined)
                    else:
                        result.append(last)
                        result.append(c)
                    last = None
                elif c not in ('>', '&', '|'):
                    result.append(c)
                else:
                    last = c
            if last:
                result.append(last)
                #logger.debug('%s -> %s', t, result)
        return result

    def peek_token(self):
        if self.peek is None:
            self.peek = self.next_token()
        return self.peek[0]

    def consume(self, tt):
        self.token = self.peek
        self.peek = self.next_token()
        if self.token[0] != tt:
            raise ValueError('consume: expected %r', tt)

    def parse(self, source, posix=None):
        self.source = source
        parse_logger.debug('starting parse of %r', source)
        if posix is None:
            posix = os.name == 'posix'
        self.lex = shell_shlex(source, posix=posix, control=True)
        self.token = None
        self.peek = None
        self.peek_token()
        result = self.parse_list()
        return result

    def parse_list(self):
        parts = [self.parse_pipeline()]
        tt = self.peek_token()
        while tt in (';', '&'):
            self.consume(tt)
            part = self.parse_pipeline()
            parts.append(Node(kind='sync', sync=tt))
            parts.append(part)
            tt = self.peek_token()
        if len(parts) == 1:
            node = parts[0]
        else:
            node = Node(kind='list', parts=parts)
        parse_logger.debug('returning %r', node)
        return node

    def parse_pipeline(self):
        parts = [self.parse_logical()]
        tt = self.peek_token()
        while tt in ('&&', '||'):
            self.consume(tt)
            part = self.parse_logical()
            parts.append(Node(kind='check', check=tt))
            parts.append(part)
            tt = self.peek_token()
        if len(parts) == 1:
            node = parts[0]
        else:
            node = Node(kind='pipeline', parts=parts)
        parse_logger.debug('returning %r', node)
        return node

    def parse_logical(self):
        tt = self.peek_token()
        if tt == '(':
            self.consume(tt)
            node = self.parse_list()
            self.consume(')')
        else:
            parts = [self.parse_command()]
            tt = self.peek_token()
            while tt in ('|', '|&'):
                last_part = parts[-1]
                if ((tt == '|' and 1 in last_part.redirects) or
                    (tt == '|&' and 2 in last_part.redirects)):
                    if last_part.redirects != SWAP_OUTPUTS:
                        raise ValueError(
                            'semantics: cannot redirect and pipe the '
                            'same stream')
                self.consume(tt)
                part = self.parse_command()
                parts.append(Node(kind='pipe', pipe=tt))
                parts.append(part)
                tt = self.peek_token()
            if len(parts) == 1:
                node = parts[0]
            else:
                node = Node(kind='logical', parts=parts)
        parse_logger.debug('returning %r', node)
        return node

    def add_redirection(self, node, fd, kind, dest):
        if fd in node.redirects:
            raise ValueError('semantics: cannot redirect stream %d twice' % fd)
        node.redirects[fd] = (kind, dest)

    def parse_command(self):
        node = self.parse_command_part()
        tt = self.peek_token()
        while tt in ('word', 'number'):
            part = self.parse_command_part()
            node.command.extend(part.command)
            for fd, v in part.redirects.items():
                self.add_redirection(node, fd, v[0], v[1])
            tt = self.peek_token()
        parse_logger.debug('returning %r', node)
        if node.redirects != SWAP_OUTPUTS:
            d = dict(node.redirects)
            d.pop(1, None)
            d.pop(2, None)
            if d:
                raise ValueError('semantics: can only redirect stdout and '
                                 'stderr, not %s' % list(d.keys()))
        return node

    def parse_command_part(self):
        node = Node(kind='command', command=[self.peek[1]], redirects={})
        if self.peek[0] == 'word':
            self.consume('word')
        else:
            self.consume('number')
        tt = self.peek_token()
        while tt in ('>', '>>'):
            num = 1     # default value
            if self.peek[2] == '':
                # > or >> seen without preceding whitespace. So see if the
                # last token is a positive integer. If it is, assume it's
                # an fd to redirect and pop it, else leave it in as part of
                # the command line.
                try:
                    try_num = int(node.command[-1])
                    if try_num > 0:
                        num = try_num
                        node.command.pop()
                except ValueError:
                    pass
            redirect_kind = tt
            self.consume(tt)
            tt = self.peek_token()
            if tt not in ('word', '&'):
                raise ValueError('syntax: expecting filename or &')
            if tt == 'word':
                redirect_target = self.peek[1]
                self.consume(tt)
            else:
                self.consume('&')
                if self.peek_token() != 'number':
                    raise ValueError('syntax: number expected after &')
                n = int(self.peek[1])
                redirect_target = ('&', n)
                self.consume('number')
            self.add_redirection(node, num, redirect_kind, redirect_target)
            tt = self.peek_token()
        parse_logger.debug('returning %r', node)
        return node

def parse_command_line(source, posix=None):
    """
    Parse a command line into an AST.

    :param source: The command line to parse.
    :type source: str
    :param posix: Whether Posix conventions are used in the lexer.
    :type posix: bool
    """
    if posix is None:
        posix = os.name == 'posix'
    return CommandLineParser().parse(source, posix=posix)
