## async.py
# Light wrapper around whatever async library pydle uses.
import functools
import itertools
import collections
import threading
import datetime

import tornado.concurrent
import tornado.ioloop


class Future(tornado.concurrent.TracebackFuture):
    """ A future. """

def coroutine(func):
    """ Decorator for coroutine functions that need to block for asynchronous operations. """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return_future = Future()

        def handle_future(future):
            # Chained futures!
            try:
                if future.exception() is not None:
                    result = gen.throw(future.exception())
                else:
                    result = gen.send(future.result())
                if isinstance(result, tuple):
                    result = parallel(*result)
                result.add_done_callback(handle_future)
            except StopIteration as e:
                return_future.set_result(getattr(e, 'value', None))
            except Exception as e:
                return_future.set_exception(e)

        # Handle initial value.
        gen = func(*args, **kwargs)
        try:
            result = next(gen)
            if isinstance(result, tuple):
                result = parallel(*result)
            result.add_done_callback(handle_future)
        except StopIteration as e:
            return_future.set_result(getattr(e, 'value', None))
        except Exception as e:
            return_future.set_exception(e)

        return return_future
    return wrapper

def parallel(*futures):
    """ Create a single future that will be completed when all the given futures are. """
    result_future = Future()
    results = collections.OrderedDict(zip(futures, itertools.repeat(None)))
    futures = list(futures)

    def done(future):
        futures.remove(future)
        results[future] = future.result()
        # All out of futures. set the result.
        if not futures:
            result_future.set_result(list(results.values()))

    for future in futures:
        future.add_done_callback(done)

    return result_future


class EventLoop:
    """ A light wrapper around what event loop mechanism pydle uses underneath. """
    EVENT_MAPPING = {
        'read': tornado.ioloop.IOLoop.READ,
        'write': tornado.ioloop.IOLoop.WRITE,
        'error': tornado.ioloop.IOLoop.ERROR
    }

    def __init__(self, io_loop=None):
        self.io_loop = io_loop or tornado.ioloop.IOLoop.current()
        self.running = False
        self.run_thread = None
        self.handlers = {}
        self._context_future = None
        self._context_depth = 0

    def __del__(self):
        self.io_loop.close()


    def register(self, fd):
        """ Register a file descriptor with this event loop. """
        self.handlers[fd] = { key: [] for key in self.EVENT_MAPPING }

    def unregister(self, fd):
        """ Unregister a file descriptor with this event loop. """
        del self.handlers[fd]


    def on_read(self, fd, callback):
        """
        Add a callback for when the given file descriptor is available for reading.
        Callback will be called with file descriptor as sole argument.
        """
        self.handlers[fd]['read'].append(callback)
        self._update_events(fd)

    def on_write(self, fd, callback):
        """
        Add a callback for when the given file descriptor is available for writing.
        Callback will be called with file descriptor as sole argument.
        """
        self.handlers[fd]['write'].append(callback)
        self._update_events(fd)

    def on_error(self, fd, callback):
        """
        Add a callback for when an error has occurred on the given file descriptor.
        Callback will be called with file descriptor as sole argument.
        """
        self.handlers[fd]['error'].append(callback)
        self._update_events(fd)

    def off_read(self, fd, callback):
        """ Remove read callback for given file descriptor. """
        self.handlers[fd]['read'].remove(callback)
        self._update_events(fd)

    def off_write(self, fd, callback):
        """ Remove write callback for given file descriptor. """
        self.handlers[fd]['write'].remove(callback)
        self._update_events(fd)

    def off_error(self, fd, callback):
        """ Remove error callback for given file descriptor. """
        self.handlers[fd]['error'].remove(callback)
        self._update_events(fd)

    def handles_read(self, fd, callback):
        """ Return whether or the given read callback is active for the given file descriptor. """
        return callback in self.handlers[fd]['read']

    def handles_write(self, fd, callback):
        """ Return whether or the given write callback is active for the given file descriptor. """
        return callback in self.handlers[fd]['write']

    def handles_error(self, fd, callback):
        """ Return whether or the given error callback is active for the given file descriptor. """
        return callback in self.handlers[fd]['error']


    def _update_events(self, fd):
        try:
            self.io_loop.remove_handler(fd)
        except KeyError:
            # It's okay if there are no handlers yet.
            pass

        events = 0
        for event, ident in self.EVENT_MAPPING.items():
            if self.handlers[fd][event]:
                events |= ident
        self.io_loop.add_handler(fd, self._do_on_event, events)

    def _do_on_event(self, fd, events):
        if fd not in self.handlers:
            return

        for event, ident in self.EVENT_MAPPING.items():
            if events & ident:
                for handler in self.handlers[fd][event]:
                    handler(fd)


    def on_future(self, _future, _callback, *_args, **_kwargs):
        """ Add a callback for when the given future has been resolved. """
        self.io_loop.add_future(_future, functools.partial(_callback, *_args, **_kwargs))


    def schedule(self, _callback, *_args, **_kwargs):
        """ Schedule a callback to be ran as soon as possible in this loop. """
        self.io_loop.add_callback(_callback, *_args, **_kwargs)

    def schedule_in(self, _when, _callback, *_args, **_kwargs):
        """
        Schedule a callback to be ran as soon as possible after `when` seconds have passed.
        When called from within the event loop, will return an opaque handle that can be passed to `unschedule`
        to unschedule the function.
        """
        if not isinstance(_when, datetime.timedelta):
            _when = datetime.timedelta(seconds=_when)

        if self.run_thread != threading.current_thread().ident:
            # Schedule scheduling in IOLoop thread because of thread-safety.
            self.schedule(functools.partial(self._do_schedule_in, _when, _callback, _args, _kwargs))
        else:
            return self._do_schedule_in(_when, _callback, _args, _kwargs)

    def schedule_periodically(self, _interval, _callback, *_args, **_kwargs):
        """
        Schedule a callback to be ran every `interval` seconds.
        When called from within the event loop, will return an opaque handle that can be passed to unschedule()
        to unschedule the first call of the function.
        After that, a function will stop being scheduled if it returns False or raises an Exception.
        """
        if not isinstance(_interval, datetime.timedelta):
            _interval = datetime.timedelta(seconds=_interval)

        if self.run_thread != threading.current_thread().ident:
            # Schedule scheduling in IOLoop thread because of thread-safety.
            self.schedule(functools.partial(self._do_schedule_periodically, _interval, _callback, _args, _kwargs))
        else:
            return self._do_schedule_periodically(_interval, _callback, _args, _kwargs)

    def _do_schedule_in(self, when, callback, args, kwargs):
        return self.io_loop.add_timeout(when, functools.partial(callback, *args, **kwargs))

    def _do_schedule_periodically(self, interval, callback, args, kwargs):
        # Use a wrapper function.
        return self.io_loop.add_timeout(interval, functools.partial(self._periodic_handler, interval, callback, args, kwargs))

    def _periodic_handler(self, interval, callback, args, kwargs):
        # Call callback, and schedule again if it doesn't return False.
        handle = self._do_schedule_periodically(interval, callback, args, kwargs)
        result = False

        try:
            result = callback(*args, **kwargs)
        finally:
            if result == False:
                self.io_loop.remove_timeout(handle)

    def unschedule(self, handle):
        """ Unschedule a given timeout or periodical callback. """
        self.io_loop.remove_timeout(handle)


    def run(self):
        """ Run the event loop. """
        if not self.running:
            self.running = True
            self.run_thread = threading.current_thread().ident
            self.io_loop.start()
            self.run_thread = None
            self.running = False

    def run_with(self, func):
        """ Run loop, call function, stop loop. If function returns a future, run until the future has been resolved. """
        self.running = True
        self.run_thread = threading.current_thread().ident
        self.io_loop.run_sync(func)
        self.run_thread = None
        self.running = False

    def run_until(self, future):
        """ Run until future is resolved. """
        return self.run_with(lambda: future)

    def stop(self):
        """ Stop the event loop. """
        if self.running:
            self.io_loop.stop()


