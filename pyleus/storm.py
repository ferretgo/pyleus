from __future__ import absolute_import

import argparse
from collections import deque, namedtuple
import logging
import os
import sys
import traceback

try:
    import simplejson as json
    _ = json # pyflakes
except ImportError:
    import json

DESCRIBE_OPT = "--describe"
OPTIONS_OPT = "--options"
DEFAULT_STREAM = "default"

log = logging.getLogger(__name__)

StormTuple = namedtuple('StormTuple', "id comp stream task values")


def _is_namedtuple(obj):
    return (type(obj) is type and
            issubclass(obj, tuple) and
            hasattr(obj, "_fields"))


def _serialize(obj):
    if obj is None:
        return None
    # obj is a namedtuple "class"
    elif _is_namedtuple(obj):
        return list(obj._fields)
    # obj is a list or a tuple
    return list(obj)


def _expand_output_fields(obj):
    # if single-stream notation
    if not isinstance(obj, dict):
        return {DEFAULT_STREAM: _serialize(obj)}

    # if multiple-streams notation
    for key, value in obj.items():
        obj[key] = _serialize(value)
    return obj


def is_tick(tup):
    """Tick tuples (generated by Storm; introduced 0.8) are defined as being
    from the __system component and __tick stream.
    """
    return tup.comp == '__system' and tup.stream == '__tick'


class StormWentAwayError(Exception):

    def __init__(self):
        message = "Got EOF while reading from Storm"
        super(StormWentAwayError, self).__init__(message)


class StormComponent(object):

    OUTPUT_FIELDS = None
    OPTIONS = None

    def __init__(self, input_stream=None, output_stream=None):
        """The Storm component will parse the command line in order
        to figure out if it has been queried for a description or for
        actually running."""
        super(StormComponent, self).__init__()

        if input_stream is None:
            input_stream = sys.stdin

        if output_stream is None:
            output_stream = sys.stdout

        self._input_stream = input_stream
        self._output_stream = output_stream

        self._pending_commands = deque()
        self._pending_taskids = deque()

    def describe(self):
        """Print to stdout a JSON descrption of the component.

        The java code will use the JSON descrption for topology
        cofiguration and validation.
        """
        # The same word should be used in the yaml and in ComponentSpec.
        # Note: it is lowercase
        component_type = "other"
        if isinstance(self, Bolt):
            component_type = "bolt"
        elif isinstance(self, Spout):
            component_type = "spout"

        print json.dumps({
            "type": component_type,
            "output_fields": _expand_output_fields(self.OUTPUT_FIELDS),
            "options": _serialize(self.OPTIONS)})

    def setup_component(self):
        """Storm component setup before execution. It will also
        call the initialization method implemented in the subclass."""
        self.conf, self.context = self.init_component()

        self.initialize(self.conf, self.context)

    def initialize(self, conf, context):
        """Implement in subclass"""
        pass

    def run(self):
        parser = argparse.ArgumentParser(
            add_help=False)
        parser.add_argument(
            DESCRIBE_OPT, default=False, action="store_true")
        parser.add_argument(
            OPTIONS_OPT, default=None)
        args = parser.parse_args()

        if args.describe:
            self.describe()
            return

        self.options = json.loads(args.options) if args.options else {}
        self.run_component()

    def run_component(self):
        """Implement in subclass"""
        raise NotImplementedError

    def _read_msg(self):
        """The Storm multilang protocol specifies that messages are some JSON
        followed by the string "end\n".

        It is unclear whether there is any case in which the message preceding
        "end" will span multiple lines.
        """
        lines = []

        while True:
            line = self._input_stream.readline()
            if not line:
                # Handle EOF, which usually means Storm went away
                raise StormWentAwayError()

            line = line.strip()

            if line == "end":
                break

            lines.append(line)

        msg_str = '\n'.join(lines)
        return json.loads(msg_str)

    def _msg_is_command(self, msg):
        """Storm differentiates between commands and taskids by whether the
        message is in dict or list form.
        """
        return isinstance(msg, dict)

    def _msg_is_taskid(self, msg):
        """See _msg_is_command()"""
        return isinstance(msg, list)

    def read_command(self):
        """Return the next command from the input stream, whether from the
        _pending_commands queue or the stream directly if the queue is empty.

        In that case, queue any taskids which are received until the next
        command comes in.
        """
        if self._pending_commands:
            return self._pending_commands.popleft()

        msg = self._read_msg()

        while self._msg_is_taskid(msg):
            self._pending_taskids.append(msg)
            msg = self._read_msg()

        return msg

    def read_taskid(self):
        """Like read_command(), but returns the next taskid and queues any
        commands received while reading the input stream to do so.
        """
        if self._pending_taskids:
            return self._pending_taskids.popleft()

        msg = self._read_msg()

        while self._msg_is_command(msg):
            self._pending_commands.append(msg)
            msg = self._read_msg()

        return msg

    def read_tuple(self):
        """Read and parse a command into a StormTuple object"""
        cmd = self.read_command()
        return StormTuple(
            cmd['id'], cmd['comp'], cmd['stream'], cmd['task'], cmd['tuple'])

    def _send_msg(self, msg_dict):
        """Serialize to JSON a message dictionary and write it to the output
        stream, followed by a newline and "end\n".
        """
        self._output_stream.write(json.dumps(msg_dict) + '\n')
        self._output_stream.write("end\n")
        self._output_stream.flush()

    def _create_pidfile(self, pid_dir, pid):
        open(os.path.join(pid_dir, str(pid)), 'a').close()

    def init_component(self):
        """Receive the setup_info dict from the Storm task and report back with
        our pid; also touch a pidfile in the pidDir specified in setup_info.
        """
        setup_info = self._read_msg()

        pid = os.getpid()
        self._send_msg({'pid': pid})
        self._create_pidfile(setup_info['pidDir'], pid)

        return StormConfig(setup_info['conf']), setup_info['context']

    def send_command(self, command, opts_dict=None):
        if opts_dict is not None:
            command_dict = dict(opts_dict)
            command_dict['command'] = command
        else:
            command_dict = dict(command=command)

        self._send_msg(command_dict)

    def log(self, msg):
        self.send_command('log', {
            'msg': msg,
        })

    def error(self, msg):
        self.send_command('error', {
            'msg': msg,
        })


class Bolt(StormComponent):

    def process_tuple(self, tup):
        """Implement in subclass"""
        pass

    def _process_tuple(self, tup):
        """Implement in bolt middleware

        Bolt middleware classes such as SimpleBolt should override this to
        inject functionality around tuple processing without changing the
        API for downstream bolt implementations.
        """
        return self.process_tuple(tup)

    def run_component(self):
        try:
            self.setup_component()

            while True:
                tup = self.read_tuple()
                self._process_tuple(tup)
        except StormWentAwayError as e:
            log.warning("Disconnected from Storm. Exiting.")
        except Exception as e:
            log.exception("Exception in Bolt.run")
            self.error(traceback.format_exc(e))

    def ack(self, tup):
        self.send_command('ack', {
            'id': tup.id,
        })

    def fail(self, tup):
        self.send_command('fail', {
            'id': tup.id,
        })

    def emit(self, values, stream=None, anchors=None, direct_task=None):
        """Build and send an output tuple command dict; return the tasks to
        which the tuple was sent by Storm.
        """
        assert isinstance(values, list) or isinstance(values, tuple)

        if anchors is None:
            anchors = []

        command_dict = {
            'anchors': [anchor.id for anchor in anchors],
            'tuple': values,
        }

        if stream is not None:
            command_dict['stream'] = stream

        if direct_task is not None:
            command_dict['task'] = direct_task

        self.send_command('emit', command_dict)
        return self.read_taskid()


class SimpleBolt(Bolt):
    """A Bolt that automatically acks or fails tuples.

    Implement process_tick() in a subclass to handle tick tuples with a better
    API.
    """

    def process_tick(self):
        """Implement in subclass"""
        pass

    def _process_tuple(self, tup):
        try:
            if is_tick(tup):
                self.process_tick()
            else:
                self.process_tuple(tup)
        except:
            self.fail(tup)
            raise
        else:
            self.ack(tup)


class Spout(StormComponent):

    def next_tuple(self):
        """Implement in subclass"""
        pass

    def ack(self, tup_id):
        """Implement in subclass"""
        pass

    def fail(self, tup_id):
        """Implement in subclass"""
        pass

    def _handle_command(self, msg):
        command = msg['command']

        if command == 'next':
            self.next_tuple()
        elif command == 'ack':
            self.ack(msg['id'])
        elif command == 'fail':
            self.fail(msg['id'])

    def _sync(self):
        self.send_command('sync')

    def run_component(self):
        try:
            self.setup_component()

            while True:
                msg = self.read_command()
                self._handle_command(msg)
                self._sync()
        except StormWentAwayError as e:
            log.warning("Disconnected from Storm. Exiting.")
        except Exception as e:
            log.exception("Exception in Spout.run")
            self.error(traceback.format_exc(e))

    def emit(self, values, stream=None, tup_id=None, direct_task=None):
        """Build and send an output tuple command dict; return the tasks to
        which the tuple was sent by Storm.

        tup_id should be JSON-serializable.
        """
        assert isinstance(values, list) or isinstance(values, tuple)

        command_dict = {
            'tuple': values
        }

        if stream is not None:
            command_dict['stream'] = stream

        if tup_id is not None:
            command_dict['id'] = tup_id

        if direct_task is not None:
            command_dict['task'] = direct_task

        self.send_command('emit', command_dict)
        return self.read_taskid()


class StormConfig(dict):

    def __init__(self, conf):
        super(StormConfig, self).__init__()
        self.update(conf)

    @property
    def tick_tuple_freq(self):
        """Return the tick tuple frequency for the component.

        Note: bolts that not specify a tick tuple frequency default to null,
        while for spouts are not supposed to use tick tuples.
        """
        return self.get("topology.tick.tuple.freq.secs")
