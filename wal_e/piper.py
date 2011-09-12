#!/usr/bin/env python
"""
Utilities for handling subprocesses.

Mostly necessary only because of http://bugs.python.org/issue1652.

"""

import copy
import errno
import fcntl
import gevent.socket
import os
import signal
import subprocess
import sys

from subprocess import PIPE
from cStringIO import StringIO

# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
# This is a brutal hack to make WAL-E usable while its operators are
# in a transition phase between boto with nonblocking and regular
# fork-worker based s3cmd.  In particular, the forked workers set
# BRUTAL_AVOID_NONBLOCK_HACK to True, thus avoiding the
# NonBlockPipeFileWrap wrapper around process pipes entirely.
# !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
BRUTAL_AVOID_NONBLOCK_HACK = False

class NonBlockPipeFileWrap(object):
    def __init__(self, fp):
        # Make the file nonblocking
        fcntl.fcntl(fp, fcntl.F_SETFL, os.O_NONBLOCK)
        self._fp = fp

    def read(self, size=None):
        # Some adaptation from gevent's examples/processes.py
        accum = StringIO()

        while size is None or accum.tell() < size:
            try:
                if size is None:
                    max_read = 4096
                else:
                    max_read = min(4096, size - accum.tell())
            
                chunk = self._fp.read(max_read)

                # End of the stream: leave the loop
                if not chunk:
                    break
                accum.write(chunk)
            except IOError, ex:
                if ex[0] != errno.EAGAIN:
                    raise
                sys.exc_clear()
            gevent.socket.wait_read(self._fp.fileno())

        return accum.getvalue()

    def write(self, data):
        # Some adaptation from gevent's examples/processes.py
        buf = StringIO(data)
        bytes_total = len(data)
        bytes_written = 0
        while bytes_written < bytes_total:
            try:
                # self._fp.write() doesn't return anything, so use
                # os.write.
                bytes_written += os.write(self._fp.fileno(), buf.read(4096))
            except IOError, ex:
                if ex[0] != errno.EAGAIN:
                    raise
                sys.exc_clear()
            gevent.socket.wait_write(self._fp.fileno())

    def fileno(self):
        return self._fp.fileno()

    def close(self):
        return self._fp.close()

    def flush(self):
        return self._fp.flush()

    @property
    def closed(self):
        return self._fp.closed


def subprocess_setup(f=None):
    """
    SIGPIPE reset for subprocess workaround

    Python installs a SIGPIPE handler by default. This is usually not
    what non-Python subprocesses expect.

    Calls an optional "f" first in case other code wants a preexec_fn,
    then restores SIGPIPE to what most Unix processes expect.

    http://bugs.python.org/issue1652
    http://www.chiark.greenend.org.uk/ucgi/~cjwatson/blosxom/2009-07-02-python-sigpipe.html

    """

    def wrapper(*args, **kwargs):
        if f is not None:
            f(*args, **kwargs)

        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    return wrapper


def popen_sp(*args, **kwargs):
    """
    Same as subprocess.Popen, but restores SIGPIPE

    This bug is documented (See subprocess_setup) but did not make it
    to standard library.  Could also be resolved by using the
    python-subprocess32 backport and using it appropriately (See
    'restore_signals' keyword argument to Popen)

    """

    kwargs['preexec_fn'] = subprocess_setup(kwargs.get('preexec_fn'))
    proc = subprocess.Popen(*args, **kwargs)

    # Patch up the process object to use non-blocking I/O that yields
    # to the gevent hub.
    for fp_symbol in ['stdin', 'stdout', 'stderr']:
        value = getattr(proc, fp_symbol)
        if value is not None and not BRUTAL_AVOID_NONBLOCK_HACK:
            setattr(proc, fp_symbol, NonBlockPipeFileWrap(value))

    return proc


def pipe(*args):
    """
    Takes as parameters several dicts, each with the same
    parameters passed to popen.

    Runs the various processes in a pipeline, connecting
    the stdout of every process except the last with the
    stdin of the next process.

    Adapted from http://www.enricozini.org/2009/debian/python-pipes/

    """
    if len(args) < 2:
        raise ValueError, "pipe needs at least 2 processes"
    # Set stdout=PIPE in every subprocess except the last
    for i in args[:-1]:
        i["stdout"] = subprocess.PIPE

    # Runs all subprocesses connecting stdins and stdouts to create the
    # pipeline. Closes stdouts to avoid deadlocks.
    popens = [popen_sp(**args[0])]
    for i in range(1, len(args)):
        args[i]["stdin"] = popens[i - 1].stdout
        popens.append(popen_sp(**args[i]))
        popens[i - 1].stdout.close()

    # Returns the array of subprocesses just created
    return popens


def pipe_wait(popens):
    """
    Given an array of Popen objects returned by the
    pipe method, wait for all processes to terminate
    and return the array with their return values.

    Taken from http://www.enricozini.org/2009/debian/python-pipes/

    """
    # Avoid mutating the passed copy
    popens = copy.copy(popens)
    results = [0] * len(popens)
    while popens:
        last = popens.pop(-1)
        results[len(popens)] = last.wait()
    return results