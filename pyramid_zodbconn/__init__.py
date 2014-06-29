import sys
from zodburi import resolve_uri
from ZODB import DB
from ZODB.ActivityMonitor import ActivityMonitor
from pyramid.exceptions import ConfigurationError
from pyramid.compat import text_

def get_connection(request, dbname=None):
    """
    ``request`` must be a Pyramid request object.
    
    When called with no ``dbname`` argument or a ``dbname`` argument of
    ``None``, return a connection to the primary datbase (the database set
    up as ``zodbconn.uri`` in the current configuration).

    If you're using named databases, you can obtain a connection to a named
    database by passing its name as ``dbname``.  It must be the name of a
    database (e.g. if you've added ``zodbconn.uri.foo`` to the configuration,
    it should be ``foo``).
    """
    # not a tween.  rationale: tweens don't get called until the router accepts
    # a request.  during paster shell, paster ptweens, etc, the router is
    # never invoked

    # if this all looks very strange to you, please read:
    # http://svn.zope.org/ZODB/trunk/src/ZODB/tests/multidb.txt?rev=99605&view=markup

    registry = request.registry

    primary_conn = getattr(request, '_primary_zodb_conn', None)

    if primary_conn is None:

        zodb_dbs = getattr(registry, '_zodb_databases', None)

        if zodb_dbs is None:
            raise ConfigurationError(
                'pyramid_zodbconn not included in configuration')

        primary_db = zodb_dbs.get('')

        if primary_db is None:
            raise ConfigurationError(
                'No zodbconn.uri defined in Pyramid settings')

        primary_conn = primary_db.open()

        registry.notify(ZODBConnectionOpened(primary_conn, request))

        def finished(request):
            # closing the primary also closes any secondaries opened
            registry.notify(ZODBConnectionWillClose(primary_conn, request))
            primary_conn.transaction_manager.abort()
            primary_conn.close()
            registry.notify(ZODBConnectionClosed(primary_conn, request))

        request.add_finished_callback(finished)
        request._primary_zodb_conn = primary_conn

    if dbname is None:
        return primary_conn
    
    try:
        conn = primary_conn.get_connection(dbname)
    except KeyError:
        raise ConfigurationError(
            'No zodbconn.uri.%s defined in Pyramid settings' % dbname)

    return conn

def db_from_uri(uri, dbname, dbmap, resolve_uri=resolve_uri):
    storage_factory, dbkw = resolve_uri(uri)
    dbkw['database_name'] = dbname
    storage = storage_factory()
    return DB(storage, databases=dbmap, **dbkw)

NAMED = 'zodbconn.uri.'

def get_uris(settings):
    named = []
    for k, v in settings.items():
        if k.startswith(NAMED):
            name = k[len(NAMED):]
            if not name:
                raise ConfigurationError(
                    '%s is not a valid zodbconn identifier' % k)
            named.append((name, v))
    primary = settings.get('zodbconn.uri')
    if primary is None and named:
        raise ConfigurationError(
            'Must have primary zodbconn.uri in settings containing named uris')
    if primary:
        yield '', primary
        for name, uri in named:
            yield name, uri

class ConnectionEvent(object):
    def __init__(self, conn, request):
        self.conn = conn
        self.request = request

class ZODBConnectionOpened(ConnectionEvent):
    pass

class ZODBConnectionWillClose(ConnectionEvent):
    pass

class ZODBConnectionClosed(ConnectionEvent):
    pass


def includeme(config, db_from_uri=db_from_uri, open=open):
    """
    This includeme recognizes a ``zodbconn.uri`` setting in your deployment
    settings and creates a ZODB database if it finds one.  ``zodbconn.uri``
    is the database URI or URIs (either a whitespace-delimited string, a
    carriage-return-delimed string or a list of strings).

    Database is activated with `ZODB.ActivityMonitor.ActivityMonitor`.

    It will also recognize *named* database URIs as long as an unnamed
    database is in the configuration too:

        zodbconn.uri.sessions = file:///home/project/var/Data.fs

    Use the key ``zodbconn.transferlog`` in the deployment settings to specify
    a filename to write ZODB load/store information to, or leave key's value
    blank to send to stdout.
    """
    # db_from_uri in
    databases = config.registry._zodb_databases = {}
    for name, uri in get_uris(config.registry.settings):
        db = db_from_uri(uri, name, databases)
        # ^^ side effect: populate "databases"
        db.setActivityMonitor(ActivityMonitor())
    txlog_filename = config.registry.settings.get('zodbconn.transferlog')
    if txlog_filename is not None:
        if txlog_filename.strip() == '':
            stream = sys.stdout
        else:
            stream = open(txlog_filename, 'a')
        transferlog = TransferLog(stream)
        config.add_subscriber(transferlog.start, ZODBConnectionOpened)
        config.add_subscriber(transferlog.end, ZODBConnectionWillClose)
        config.registry._transferlog = transferlog # for testing only

class TransferLog(object):
    def __init__(self, stream):
        self.stream = stream
        self.requests = {}

    def start(self, event):
        request_id = id(event.request)
        info = self.requests[request_id] = {}
        info['loads'], info['stores'] = event.conn.getTransferCounts()

    def end(self,  event):
        request_id = id(event.request)
        info = self.requests.pop(request_id, None)
        if info is not None:
            loads_after, stores_after = event.conn.getTransferCounts()
            loads = loads_after - info['loads']
            stores = stores_after - info['stores']
            request_method = event.request.method
            url = event.request.path_qs
            value = '"%s","%s",%d,%d\n'  % (request_method, url, loads, stores)
            self.stream.write(text_(value))
