import asyncio
import logging
import weakref


async def iter_to_aiter(iter):
    """
    This converts a regular iterator to an async iterator
    """
    for _ in iter:
        yield _


def aiter_to_iter(aiter, loop=None):
    """
    Convert an async iterator to a regular iterator by invoking
    run_until_complete repeatedly.
    """
    if loop is None:
        loop = asyncio.get_event_loop()
    underlying_aiter = aiter.__aiter__()
    while True:
        try:
            _ = loop.run_until_complete(underlying_aiter.__anext__())
            yield _
        except StopAsyncIteration:
            break


class linked_aiter:
    """
    This can be shared by multiple consumers.
    """
    def __init__(self, tail=None, next_callback_f=None):
        self._tail = tail or asyncio.Future()
        self._lock = asyncio.Semaphore()
        self._next_callback_f = next_callback_f

    def split(self, is_active=True):
        """
        Make a copy of the iterator. If "is_active" is False, we
        will never call the "empty_callback_f", so it will be a purely
        passive, observing copy, like listening in on a wire without affecting it.
        """
        next_callback_f = self._next_callback_f if is_active else None
        return linked_aiter(self.tail, next_callback_f)

    def __aiter__(self):
        return self

    async def __anext__(self):
        async with self._lock:
            try:
                _, self._tail = await self._tail
                if self._next_callback_f and not self._tail.done():
                    self._next_task = asyncio.ensure_future(self._next_callback_f(self._tail))
                return _
            except asyncio.CancelledError:
                raise StopAsyncIteration


class push_aiter(linked_aiter):
    """
    This is a linked_aiter that allows pushing of elements.
    """
    def __init__(self, advance_callback_f=None):
        super(push_aiter, self).__init__(asyncio.Future())
        self._head = self._tail

    async def push(self, item):
        self.push_nowait(item)

    def push_nowait(self, item):
        if self._head.cancelled():
            raise ValueError("%s closed" % self)
        new_head = asyncio.Future()
        self._head.set_result((item, new_head))
        self._head = new_head

    def stop(self):
        if not self._head.done():
            self._head.cancel()


class q_aiter:
    """
    Creates an async iterator that you can "push" items into.
    Call "stop" when no more items will be added to the queue, so the iterator
    knows to end.
    """
    def __init__(self, q=None, maxsize=1, full_callback=None):
        if q is None:
            q = asyncio.Queue(maxsize=maxsize)
        self._q = q
        self._stopping = asyncio.Future()
        self._stopped = False
        self._full_callback = full_callback

    def stop(self):
        """
        No more items will be added to the queue. Items in queue will be processed,
        then a StopAsyncIteration raised.
        """
        if not self._stopping.done():
            self._stopping.set_result(True)

    def q(self):
        return self._q

    async def push(self, *items):
        for _ in items:
            if self._stopping.done():
                raise ValueError("%s closed" % self)
            if self._full_callback and self._q.full():
                self._full_callback(self, _)
            await self._q.put(_)

    def push_nowait(self, *items):
        for _ in items:
            if self._stopping.done():
                raise ValueError("%s closed" % self)
            if self.full():
                if self._full_callback:
                    self._full_callback(self, _)
            else:
                self._q.put_nowait(_)

    def full(self):
        return self._q.full()

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            if self._stopping.done():
                if self._stopped:
                    raise StopAsyncIteration
                try:
                    return self._q.get_nowait()
                except asyncio.queues.QueueEmpty:
                    self._stopped = True
                    raise StopAsyncIteration

            q_get = asyncio.ensure_future(self._q.get())
            done, pending = await asyncio.wait([self._stopping, q_get], return_when=asyncio.FIRST_COMPLETED)
            if q_get in done:
                return q_get.result()
            q_get.cancel()

    def __repr__(self):
        return "<q_aiter %s>" % self._q


class sharable_aiter:
    """
    Pipe an iterator through a queue to ensure that it can be shared by multiple consumers.

    This creates a task to monitor the main iterator, plus a task for each active
    iterator that has come out of the main iterator.
    """
    def __init__(self, aiter):
        self._aiter = aiter.__aiter__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._aiter.__anext__()


async def join_aiters(aiter_of_aiters):
    """
    Takes an iterator of async iterators and pipe them into a single async iterator.

    This creates a task to monitor the main iterator, plus a task for each active
    iterator that has come out of the main iterator.
    """

    async def aiter_to_next_job(aiter):
        """
        Return items to add to stack, plus jobs to add to queue.
        """
        try:
            v = await aiter.__anext__()
            return [v], [asyncio.ensure_future(aiter_to_next_job(aiter))]
        except StopAsyncIteration:
            return [], []

    async def main_aiter_to_next_job(aiter_of_aiters):
        try:
            new_aiter = await aiter_of_aiters.__anext__()
            return [], [asyncio.ensure_future(aiter_to_next_job(new_aiter.__aiter__())), asyncio.ensure_future(main_aiter_to_next_job(aiter_of_aiters))]
        except StopAsyncIteration:
            return [], []

    jobs = set([main_aiter_to_next_job(aiter_of_aiters.__aiter__())])

    while jobs:
        done, jobs = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        for _ in done:
            new_items, new_jobs = await _
            for _ in new_items:
                yield _
            jobs.update(_ for _ in new_jobs)


async def map_aiter(map_f, aiter):
    """
    Take an async iterator and a map function, and apply the function
    to everything coming out of the iterator before passing it on.
    """
    if asyncio.iscoroutinefunction(map_f):
        async for _ in aiter:
            try:
                yield await map_f(_)
            except Exception:
                logging.exception("unhandled mapping function %s worker exception on %s", map_f, _)
    else:
        async for _ in aiter:
            try:
                yield map_f(_)
            except Exception:
                logging.exception("unhandled mapping function %s worker exception on %s", map_f, _)


async def flatten_aiter(aiter):
    """
    Take an async iterator that returns lists and return the individual
    elements.
    """
    async for items in aiter:
        try:
            for _ in items:
                yield _
        except Exception:
            pass


async def map_filter_aiter(map_f, aiter):
    """
    In this case, the map_f must return a list, which will be flattened.
    You can filter items by returning an empty list.
    """
    if asyncio.iscoroutinefunction(map_f):
        async for _ in aiter:
            try:
                items = await map_f(_)
                for _ in items:
                    yield _
            except Exception:
                logging.exception("unhandled mapping function %s worker exception on %s", map_f, _)
    else:
        async for _ in aiter:
            try:
                items = map_f(_)
                for _ in items:
                    yield _
            except Exception:
                logging.exception("unhandled mapping function %s worker exception on %s", map_f, _)


def parallel_map_aiter(map_f, worker_count, aiter, q=None, maxsize=1):
    shared_aiter = sharable_aiter(aiter)
    aiters = [map_aiter(map_f, shared_aiter) for _ in range(worker_count)]
    return join_aiters(iter_to_aiter(aiters))


class _active_forked_aiter(q_aiter):
    def __init__(self, empty_callback_f, q=None, maxsize=0):
        super(_active_forked_aiter, self).__init__(q=q, maxsize=maxsize)
        self._empty_callback_f = empty_callback_f

    async def __anext__(self):
        while self._q.empty() and not self._stopping.done():
            await self._empty_callback_f()
        return await super(_active_forked_aiter, self).__anext__()


class aiter_forker:
    """
    This class wraps an aiter and allows forks. Each fork gets
    an identical copy of elements of aiter.

    There are two kinds of forks: active and passive. Passive forks never
    query the original aiter, but instead, wait for an active fork to query it.
    It's like to "listening in" on a wire without affecting its flow.
    """

    def __init__(self, aiter):
        self._open_aiter = aiter.__aiter__()
        self._outputs = weakref.WeakSet()
        self._is_fetching = asyncio.Semaphore()

    async def _fetch_next(self):
        if self._is_fetching.locked():
            async with self._is_fetching:
                return
        async with self._is_fetching:
            try:
                _ = await self._open_aiter.__anext__()
            except StopAsyncIteration:
                for output in list(self._outputs):
                    output.stop()
                return
            for output in list(self._outputs):
                output.push_nowait(_)

    def new_fork(self, q=None, maxsize=0, is_active=False):
        if is_active:
            aiter = _active_forked_aiter(self._fetch_next, q=q, maxsize=maxsize)
        else:
            aiter = q_aiter(q=q, maxsize=maxsize)
        self._outputs.add(aiter)
        return aiter

    def remove_fork(self, aiter):
        self._outputs.discard(aiter)


def rated_aiter(rate_limiter, aiter):
    """
    Returns a pair: an iter along with a function that you can "push"
    integer values into.

    This is kind of like an electronic transistor, except discrete.
    """
    r_aiter = map_aiter(lambda x: x[0], azip(aiter, map_filter_aiter(range, rate_limiter)))
    return r_aiter


async def azip(*aiters):
    """
    async version of zip
    example:
        async for a, b, c in azip(aiter1, aiter2, aiter3):
            print(a, b, c)
    """
    anext_list = [_.__aiter__() for _ in aiters]
    while True:
        try:
            next_list = [await _.__anext__() for _ in anext_list]
        except StopAsyncIteration:
            break
        yield tuple(next_list)
