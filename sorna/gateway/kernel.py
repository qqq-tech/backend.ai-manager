'''
Kernel session management.
'''

import asyncio
import base64
from collections import defaultdict
from datetime import datetime
import functools
import logging
import time
import uuid
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
import aiozmq
from aiozmq import create_zmq_stream as aiozmq_sock
from async_timeout import timeout as _timeout
from dateutil.tz import tzutc
import simplejson as json
import sqlalchemy as sa
import zmq

from sorna.exceptions import ServiceUnavailable, InvalidAPIParameters, QuotaExceeded, \
                             QueryNotImplemented, InstanceNotFound, KernelNotFound, SornaError  # noqa
from . import GatewayStatus
from .auth import auth_required
from .models import KeyPair, Usage
from ..manager.registry import InstanceRegistry

_json_type = 'application/json'

log = logging.getLogger('sorna.gateway.kernel')

grace_events = []


@auth_required
async def create(request):
    if request.app['status'] != GatewayStatus.RUNNING:
        raise ServiceUnavailable('Server not ready.')
    try:
        with _timeout(2):
            params = await request.json()
        log.info(f"GET_OR_CREATE (lang:{params['lang']}, token:{params['clientSessionToken']})")
        assert 8 <= len(params['clientSessionToken']) <= 80
    except (asyncio.TimeoutError, AssertionError,
            KeyError, json.decoder.JSONDecodeError) as e:
        log.warn(f'GET_OR_CREATE: invalid/missing parameters, {e!r}')
        raise InvalidAPIParameters
    resp = {}
    try:
        access_key = request.keypair['access_key']
        concurrency_limit = request.keypair['concurrency_limit']
        async with request.app.dbpool.acquire() as conn, conn.transaction():
            query = sa.select([KeyPair.c.concurrency_used], for_update=True) \
                      .select_from(KeyPair) \
                      .where(KeyPair.c.access_key == access_key)  # noqa
            concurrency_used = await conn.fetchval(query)
            log.debug(f'access_key: {access_key} ({concurrency_used} / {concurrency_limit})')
            if concurrency_used >= concurrency_limit:
                raise QuotaExceeded
            kernel, created = await request.app['registry'].get_or_create_kernel(
                params['clientSessionToken'], params['lang'], access_key)
            resp['kernelId'] = kernel.id
            if created:
                query = sa.update(KeyPair) \
                          .values(concurrency_used=KeyPair.c.concurrency_used + 1) \
                          .where(KeyPair.c.access_key == access_key)  # noqa
                await conn.fetchval(query)
    except SornaError:
        log.exception('GET_OR_CREATE: API Internal Error')
        raise
    return web.Response(status=201, content_type=_json_type,
                        text=json.dumps(resp))


def grace_event_catcher(func):
    '''
    Catches events during grace periods and prevent event handlers from running.
    '''
    @functools.wraps(func)
    async def wrapped(*args, **kwargs):
        app = args[0]
        if app['status'] == GatewayStatus.STARTING:
            evinfo = {
                '_type': func.__name__,
                '_handler': func,
                '_when': time.monotonic(),
                '_args': args[1:],
                '_kwargs': kwargs,
            }
            grace_events.append(evinfo)
        else:
            return (await func(*args, **kwargs))
    return wrapped


async def update_instance_usage(app, inst_id):
    # In heartbeat timeouts, we do NOT clear Redis keys because
    # the timeout may be a transient one.
    kern_ids = await app['registry'].get_kernels_in_instance(inst_id)
    kernels = await app['registry'].get_kernels(kern_ids)
    affected_keys = [kern.access_key for kern in kernels if kern is not None]
    await app['registry'].update_instance(inst_id, {'status': 'lost'})

    # TODO: enqueue termination event to streaming response queue

    per_key_counts = defaultdict(int)
    for ak in filter(lambda ak: ak is not None, affected_keys):
        per_key_counts[ak] += 1
    log.warning(f' -> cleaning {kern_ids!r} {per_key_counts}')

    if not affected_keys:
        return

    async with app.dbpool.acquire() as conn, conn.transaction():
        log.debug(f'update_instance_usage({inst_id})')
        for kern in kernels:
            if kern is None:
                continue
            query = Usage.insert().values(**{
                'id': uuid.uuid4(),
                'access_key': kern.access_key,
                'kernel_type': kern.lang,
                'kernel_id': kern.id,
                'started_at': kern.created_at,
                'terminated_at': datetime.now(tzutc()),
                'cpu_used': kern.cpu_used,
                'mem_used': kern.mem_max_bytes // 1024,
                'io_used': (kern.io_read_bytes + kern.io_write_bytes) // 1024,
                'net_used': (kern.net_rx_bytes + kern.net_tx_bytes) // 1024,
            })
            await conn.execute(query)
            query = sa.update(KeyPair) \
                      .values(concurrency_used=KeyPair.c.concurrency_used
                                               - per_key_counts[kern.access_key]) \
                      .where(KeyPair.c.access_key == kern.access_key)  # noqa
            await conn.fetchval(query)


async def update_kernel_usage(app, kern_id, kern_stat=None):

    # TODO: enqueue termination event to streaming response queue

    try:
        kern = await app['registry'].get_kernel(kern_id)
    except KernelNotFound:
        log.warning(f'update_kernel_usage({kern_id}): kernel is missing!')
        return

    async with app.dbpool.acquire() as conn, conn.transaction():
        query = sa.update(KeyPair) \
                  .values(concurrency_used=KeyPair.c.concurrency_used - 1) \
                  .where(KeyPair.c.access_key == kern.access_key)  # noqa
        await conn.fetchval(query)
        if kern_stat:
            # if last stats available, use it.
            log.info(f'update_kernel_usage: last-stat: {kern_stat}')
            query = Usage.insert().values(**{
                'access_key_id': kern.access_key,
                'kernel_type': kern.lang,
                'kernel_id': kern.id,
                'started_at': kern.created_at,
                'terminated_at': datetime.now(tzutc()),
                'cpu_used': kern_stat['cpu_used'],
                'mem_used': kern_stat['mem_max_bytes'] // 1024,
                'io_used': (kern_stat['io_read_bytes'] + kern_stat['io_write_bytes']) // 1024,
                'net_used': (kern_stat['net_rx_bytes'] + kern_stat['net_tx_bytes']) // 1024,
            })
        else:
            # otherwise, get the latest stats from the registry.
            log.info(f'update_kernel_usage: registry-stat')
            query = Usage.insert().values(**{
                'access_key_id': kern.access_key,
                'kernel_type': kern.lang,
                'kernel_id': kern.id,
                'started_at': kern.created_at,
                'terminated_at': datetime.now(tzutc()),
                'cpu_used': kern.cpu_used,
                'mem_used': kern.mem_max_bytes // 1024,
                'io_used': (kern.io_read_bytes + kern.io_write_bytes) // 1024,
                'net_used': (kern.net_rx_bytes + kern.net_tx_bytes) // 1024,
            })
        await conn.execute(query)


@grace_event_catcher
async def kernel_terminated(app, kern_id, reason, kern_stat):
    for handler in app['stream_pty_handlers'][kern_id]:
        handler.cancel()
    await update_kernel_usage(app, kern_id, kern_stat)
    await app['registry'].forget_kernel(kern_id)


@grace_event_catcher
async def instance_started(app, inst_id):
    await app['registry'].reset_instance(inst_id)


@grace_event_catcher
async def instance_terminated(app, inst_id, reason):
    if reason == 'agent-lost':
        log.warning(f'agent@{inst_id} heartbeat timeout detected.')
        await update_instance_usage(app, inst_id)
        for kern_id in (await app['registry'].get_kernels_in_instance(inst_id)):
            for handler in app['stream_pty_handlers'][kern_id]:
                handler.cancel()
                await handler
        await app['registry'].forget_all_kernels_in_instance(inst_id)
    else:
        # On normal instance termination, kernel_terminated events were already
        # triggered by the agent.
        pass
    await app['registry'].forget_instance(inst_id)


@grace_event_catcher
async def instance_heartbeat(app, inst_id, inst_info, running_kernels, interval):
    revived = False
    try:
        inst_status = await app['registry'].get_instance(inst_id, 'status')
        if inst_status == 'lost':
            revived = True
    except InstanceNotFound:
        # may have started during the grace period.
        app['event_server'].local_dispatch('instance_started', inst_id)

    if revived:
        log.warning(f'agent@{inst_id} revived.')
        await app['registry'].revive_instance(inst_id, inst_info['addr'])
    else:
        await app['registry'].handle_heartbeat(inst_id, inst_info, running_kernels, interval)


# NOTE: This event is ignored during the grace period.
async def instance_stats(app, inst_id, kern_stats, interval):
    if app['status'] == GatewayStatus.RUNNING:
        await app['registry'].handle_stats(inst_id, kern_stats, interval)


async def collect_agent_events(app, heartbeat_interval):
    '''
    Collects agent-generated events for a while (via :func:`grace_event_catcher`).
    This allows synchronization of Redis/DB with the current cluster status
    upon (re)starts of the gateway.
    '''

    log.info('running a grace period to detect live agents...')
    app['status'] = GatewayStatus.STARTING
    grace_events.clear()

    await asyncio.sleep(heartbeat_interval * 2.1)

    per_inst_events = defaultdict(list)
    processed_events = []

    for ev in grace_events:
        if ev['_type'] in ('instance_started', 'instance_terminated', 'instance_heartbeat'):
            per_inst_events[ev['_args'][0]].append(ev)

    # Keep only the latest event for each instance.
    for inst_id, events in per_inst_events.items():
        last_event = max(events, key=lambda v: v['_when'])
        processed_events.append(last_event)
        # TODO: sometimes the restarted gateway receives duplicate "instance_terminated" events...

    # Mark instances not detected during event collection to be cleared.
    valid_inst_ids = set(ev['_args'][0] for ev in processed_events)
    terminated_inst_ids = [
        inst_id async for inst_id in app['registry'].enumerate_instances(check_shadow=False)
        if inst_id not in valid_inst_ids and inst_id is not None
    ]

    log.info('bulk-dispatching latest events...')
    app['status'] = GatewayStatus.SYNCING

    for inst_id in terminated_inst_ids:
        log.warning(f'instance {inst_id} is not running!')
        await update_instance_usage(app, inst_id)
        await app['registry'].forget_instance(inst_id)

    for ev in processed_events:
        # calculate & update diff on kernel list and usage
        if ev['_type'] == 'instance_heartbeat':
            inst_id = ev['_args'][0]
            inst_info = ev['_args'][1]
            running_kernels = set(ev['_args'][2])
            tracked_kernels = set(await app['registry'].get_kernels_in_instance(inst_id))
            new_kernels = running_kernels - tracked_kernels
            old_kernels = tracked_kernels - running_kernels
            if new_kernels:
                log.warning(f'bulk-sync: new untracked kernels on {inst_id}: {new_kernels}')
            if old_kernels:
                log.warning(f'bulk-sync: deleted tracked kernels on {inst_id}: {old_kernels}')
            for kern_id in old_kernels:
                await update_kernel_usage(app, kern_id)
                await app['registry'].forget_kernel(kern_id)
            async with app.dbpool.acquire() as conn, conn.transaction():
                # This case should be very very rare.
                for kern_id in new_kernels:
                    access_key = await app['registry'].get_kernel(kern_id, 'access_key')
                    query = sa.update(KeyPair) \
                              .values(concurrency_used=KeyPair.c.concurrency_used + 1) \
                              .where(KeyPair.c.access_key == access_key)  # noqa
                    await conn.fetchval(query)

        # invoke original event handler
        await ev['_handler'](app, *ev['_args'], **ev['_kwargs'])

    log.info('entering normal operation mode...')
    app['status'] = GatewayStatus.RUNNING


@auth_required
async def destroy(request):
    if request.app['status'] != GatewayStatus.RUNNING:
        raise ServiceUnavailable('Server not ready.')
    kern_id = request.match_info['kernel_id']
    log.info(f'DESTROY (k:{kern_id})')
    try:
        await request.app['registry'].destroy_kernel(kern_id)
    except SornaError:
        log.exception('DESTROY: API Internal Error')
        raise
    return web.Response(status=204)


@auth_required
async def get_info(request):
    if request.app['status'] != GatewayStatus.RUNNING:
        raise ServiceUnavailable('Server not ready.')
    resp = {}
    kern_id = request.match_info['kernel_id']
    log.info(f'GETINFO (k:{kern_id})')
    try:
        kern = await request.app['registry'].get_kernel(kern_id)
        await request.app['registry'].update_kernel(kern_id, {
            'num_queries': int(kern.num_queries) + 1,
        })
        resp['lang'] = kern.lang
        age = datetime.now(tzutc()) - kern.created_at
        resp['age'] = age.total_seconds() * 1000
        # Resource limits collected from agent heartbeats
        # TODO: factor out policy/image info as a common repository
        resp['queryTimeout']  = int(kern.exec_timeout * 1000)
        resp['idleTimeout']   = int(kern.idle_timeout * 1000)
        resp['memoryLimit']   = kern.mem_limit
        resp['maxCpuCredit']  = int(kern.exec_timeout * 1000)
        # Stats collected from agent heartbeats
        resp['numQueriesExecuted'] = kern.num_queries
        resp['idle']          = int(kern.idle * 1000)
        resp['memoryUsed']    = kern.mem_max_bytes // 1024
        resp['cpuCreditUsed'] = kern.cpu_used
        log.info(f'information retrieved: {resp!r}')
    except SornaError:
        log.exception('GETINFO: API Internal Error')
        raise
    return web.Response(status=200, content_type=_json_type,
                        text=json.dumps(resp))


@auth_required
async def restart(request):
    if request.app['status'] != GatewayStatus.RUNNING:
        raise ServiceUnavailable('Server not ready.')
    kern_id = request.match_info['kernel_id']
    log.info(f'RESTART (k:{kern_id})')
    try:
        kern = await request.app['registry'].get_kernel(kern_id)
        await request.app['registry'].update_kernel(kern_id, {
            'num_queries': int(kern.num_queries) + 1,
        })
        await request.app['registry'].restart_kernel(kern_id)
        for sock in request.app['stream_stdin_socks'][kern_id]:
            sock.close()
    except SornaError:
        log.exception('RESTART: API Internal Error')
        raise
    except:
        log.exception('RESTART: unexpected error')
        raise web.HTTPInternalServerError
    return web.Response(status=204)


@auth_required
async def execute_snippet(request):
    if request.app['status'] != GatewayStatus.RUNNING:
        raise ServiceUnavailable('Server not ready.')
    resp = {}
    kern_id = request.match_info['kernel_id']
    try:
        with _timeout(2):
            params = await request.json()
        log.info(f'EXECUTE_SNIPPET (k:{kern_id})')
    except (asyncio.TimeoutError, json.decoder.JSONDecodeError):
        log.warn('EXECUTE_SNIPPET: invalid/missing parameters')
        raise InvalidAPIParameters
    try:
        kern = await request.app['registry'].get_kernel(kern_id)
        await request.app['registry'].update_kernel(kern_id, {
            'num_queries': int(kern.num_queries) + 1,
        })
        resp['result'] = await request.app['registry'].execute_snippet(
            kern_id, params['codeId'], params['code'])
    except SornaError:
        log.exception('EXECUTE_SNIPPET: API Internal Error')
        raise
    return web.Response(status=200, content_type=_json_type,
                        text=json.dumps(resp))


# TODO: @auth_required
async def stream_pty(request):
    app = request.app
    kern_id = request.match_info['kernel_id']
    try:
        kernel = await app['registry'].get_kernel(kern_id)
    except KernelNotFound:
        raise

    # Upgrade connection to WebSocket.
    ws = web.WebSocketResponse()
    if not ws.can_prepare(request):
        raise web.HTTPUpgradeRequired
    await ws.prepare(request)

    app['stream_pty_handlers'][kern_id].add(asyncio.Task.current_task())

    async def connect_streams(kernel):
        kernel_ip = urlparse(kernel.addr).hostname
        stdin_addr = f'tcp://{kernel_ip}:{kernel.stdin_port}'
        log.debug(f'stream_pty({kern_id}): stdin: {stdin_addr}')
        stdin_sock = await aiozmq_sock(zmq.PUB, connect=stdin_addr)
        stdin_sock.transport.setsockopt(zmq.LINGER, 100)
        stdout_addr = f'tcp://{kernel_ip}:{kernel.stdout_port}'
        log.debug(f'stream_pty({kern_id}): stdout: {stdout_addr}')
        stdout_sock = await aiozmq_sock(zmq.SUB, connect=stdout_addr)
        stdout_sock.transport.setsockopt(zmq.LINGER, 100)
        stdout_sock.transport.subscribe(b'')
        return stdin_sock, stdout_sock

    # Wrap sockets in a list so that below coroutines can share reference changes.
    socks = list(await connect_streams(kernel))
    app['stream_stdin_socks'][kern_id].add(socks[0])
    stream_sync = asyncio.Event()

    async def stream_stdin():
        nonlocal socks
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data['type'] == 'stdin':
                        raw_data = base64.b64decode(data['chars'].encode('ascii'))
                        try:
                            socks[0].write([raw_data])
                        except (AttributeError, aiozmq.ZmqStreamClosed):
                            # AttributeError occurs when stdin_sock._transport is None
                            # because it's already closed somewhere else.
                            app['stream_stdin_socks'][kern_id].remove(socks[0])
                            socks[1].close()
                            kernel = await app['registry'].get_kernel(kern_id)
                            stdin_sock, stdout_sock = await connect_streams(kernel)
                            socks[0] = stdin_sock
                            socks[1] = stdout_sock
                            app['stream_stdin_socks'][kern_id].add(socks[0])
                            socks[0].write([raw_data])
                            log.debug(f'stream_stdin({kern_id}): zmq stream reset')
                            stream_sync.set()
                            continue
                    else:
                        kernel = await app['registry'].get_kernel(kern_id)
                        await request.app['registry'].update_kernel(kern_id, {
                            'num_queries': int(kernel.num_queries) + 1,
                        })
                        if data['type'] == 'resize':
                            code = f"%resize {data['rows']} {data['cols']}"
                            await app['registry'].execute_snippet(kern_id, '', code)
                        elif data['type'] == 'ping':
                            await app['registry'].execute_snippet(kern_id, '', '%ping')
                        elif data['type'] == 'restart':
                            # Close existing zmq sockets and let stream handlers get a new one
                            # with changed stdin/stdout ports.
                            if not socks[0].at_closing():
                                await app['registry'].restart_kernel(kern_id)
                                socks[0].close()
                            else:
                                log.warning(f'stream_stdin({kern_id}): duplicate kernel restart request; ignoring it.')
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.warning(f'stream_stdin({kern_id}): '
                                f'connection closed ({ws.exception()})')
        except asyncio.CancelledError:
            # Agent or kernel is terminated.
            pass
        except:
            log.exception(f'stream_stdin({kern_id}): unexpected error')
        finally:
            log.debug(f'stream_stdin({kern_id}): terminated')
            if not socks[0].at_closing():
                socks[0].close()

    async def stream_stdout():
        nonlocal socks
        log.debug(f'stream_stdout({kern_id}): started')
        try:
            while True:
                try:
                    data = await socks[1].read()
                except aiozmq.ZmqStreamClosed:
                    await stream_sync.wait()
                    stream_sync.clear()
                    log.debug(f'stream_stdout({kern_id}): zmq stream reset')
                    continue
                if ws.closed:
                    break
                ws.send_str(json.dumps({
                    'type': 'out',
                    'data': base64.b64encode(data[0]).decode('ascii'),
                }, ensure_ascii=False))
        except asyncio.CancelledError:
            pass
        except:
            log.exception(f'stream_stdout({kern_id}): unexpected error')
        finally:
            log.debug(f'stream_stdout({kern_id}): terminated')
            socks[1].close()

    # According to aiohttp docs, reading ws must be done inside this task.
    # We execute the stdout handler as another task.
    try:
        stdout_task = asyncio.ensure_future(stream_stdout())
        await stream_stdin()
    except:
        log.exception(f'stream_pty({kern_id}): unexpected error')
    finally:
        app['stream_pty_handlers'][kern_id].remove(asyncio.Task.current_task())
        app['stream_stdin_socks'][kern_id].remove(socks[0])
        stdout_task.cancel()
        await stdout_task
    return ws


@auth_required
async def stream_events(request):
    kern_id = request.match_info['kernel_id']
    # TODO: dequeue the streaming response queue for the given kernel id
    raise QueryNotImplemented


async def init(app):
    app.router.add_route('POST', '/v1/kernel/create', create)
    app.router.add_route('GET', '/v1/kernel/{kernel_id}', get_info)
    app.router.add_route('PATCH', '/v1/kernel/{kernel_id}', restart)
    app.router.add_route('DELETE', '/v1/kernel/{kernel_id}', destroy)
    app.router.add_route('POST', '/v1/kernel/{kernel_id}', execute_snippet)
    app.router.add_route('GET', '/v1/stream/kernel/{kernel_id}/pty', stream_pty)
    app.router.add_route('GET', '/v1/stream/kernel/{kernel_id}/events', stream_events)

    app['event_server'].add_handler('kernel_terminated', kernel_terminated)
    app['event_server'].add_handler('instance_started', instance_started)
    app['event_server'].add_handler('instance_terminated', instance_terminated)
    app['event_server'].add_handler('instance_heartbeat', instance_heartbeat)
    app['event_server'].add_handler('instance_stats', instance_stats)

    app['stream_pty_handlers'] = defaultdict(set)
    app['stream_stdin_socks'] = defaultdict(set)

    app['registry'] = InstanceRegistry(app.config.redis_addr)
    await app['registry'].init()

    heartbeat_interval = 3.0
    asyncio.ensure_future(collect_agent_events(app, heartbeat_interval))


async def shutdown(app):
    for per_kernel_handlers in app['stream_pty_handlers'].values():
        for handler in per_kernel_handlers:
            handler.cancel()
    await app['registry'].terminate()
