import logging
import os
import urllib
import threading
import sys
from Queue import Queue
from flask import Flask, redirect, url_for, abort, request
from sqlalchemy.orm import scoped_session
from sqlalchemy.orm.session import sessionmaker
from flexget.event import fire_event
from flexget.plugin import PluginDependencyError
from flexget.logger import FlexGetFormatter

log = logging.getLogger('webui')

app = Flask(__name__)
manager = None
db_session = None
server = None
executor = None

_home = None
_menu = []


class BufferQueue(Queue):

    def write(self, txt):
        txt = txt.rstrip('\n')
        if txt:
            self.put_nowait(txt)


class ExecThread(threading.Thread):
    """Thread that does the execution. It can accept options with an execution, and queues execs if necessary."""

    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = Queue()

    def run(self):
        while True:
            kwargs = self.queue.get() or {}
            opts = kwargs.get('options')
            output = kwargs.get('output')
            # Store the managers options and current stdout to be restored after our execution
            if opts:
                old_opts = manager.options
                manager.options = opts
            if output:
                old_stdout = sys.stdout
                old_stderr = sys.stderr
                sys.stdout = output
                sys.stderr = output
                streamhandler = logging.StreamHandler(output)
                streamhandler.setFormatter(FlexGetFormatter())
                logging.getLogger().addHandler(streamhandler)
                self.queue.all_tasks_done
            try:
                manager.execute()
            finally:
                # Inform queue we are done processing this item.
                self.queue.task_done()
                # Restore manager's previous options and stdout
                if opts:
                    manager.options = old_opts
                if output:
                    print 'EOF'
                    sys.stdout = old_stdout
                    sys.stderr = old_stderr
                    logging.getLogger().removeHandler(streamhandler)

    def execute(self, **kwargs):
        """
        Adds an execution to the queue.

        keyword arguments:
        options: Values from an OptionParser to be used for this execution
        output: a BufferQueue object that will be filled with output from the execution.
        """
        if kwargs.get('output') and self.queue.unfinished_tasks:
            kwargs['output'].write('There is already an execution running. ' +
                                   'This execution will start when the previous completes.')
        self.queue.put_nowait(kwargs)
        self.queue.unfinished_tasks


def _update_menu(root):
    """Iterates trough menu navigation and sets the item selected based on the :root:"""
    for item in _menu:
        if item['href'].startswith(root):
            item['current'] = True
            log.debug('current menu item %s' % root)
        else:
            if 'current' in item:
                item.pop('current')


@app.route('/')
def start():
    """Redirect user to registered home plugin"""
    if not _home:
        abort(404)
    return redirect(url_for(_home))


@app.context_processor
def flexget_variables():
    path = urllib.splitquery(request.path)[0]
    root = '/' + path.split('/', 2)[1]
    # log.debug('root is: %s' % root)
    _update_menu(root)
    return {'menu': _menu, 'manager': manager}


def load_ui_plugins():

    # TODO: load from proper paths

    d = 'flexget/ui/plugins'
    import imp
    valid_suffixes = [suffix for suffix, mod_type, flags in imp.get_suffixes()
                      if flags in (imp.PY_SOURCE, imp.PY_COMPILED)]

    plugin_names = set()
    for f in os.listdir(d):
        path = os.path.join(d, f, '__init__.py')
        if os.path.isfile(path):
            plugin_names.add(f)

    for name in plugin_names:
        try:
            log.info('Loading UI plugin %s' % name)
            exec "import flexget.ui.plugins.%s" % name
        except PluginDependencyError, e:
            # plugin depends on another plugin that was not imported successfully
            log.error(e.value)
        except Exception, e:
            log.critical('Exception while loading plugin %s' % name)
            log.exception(e)
            raise


def register_plugin(plugin, url_prefix=None, menu=None, order=128, home=False):
    """Registers UI plugin.
    :plugin: Flask Module instance for the plugin
    """

    log.info('Registering UI plugin %s' % plugin.name)
    url_prefix = url_prefix or '/' + plugin.name
    app.register_module(plugin, url_prefix=url_prefix)
    if menu:
        register_menu(url_prefix, menu, order=order)
    if home:
        register_home(plugin.name + '.index')


def register_menu(href, caption, order=128):
    global _menu
    _menu.append({'href': href, 'caption': caption, 'order': order})
    _menu = sorted(_menu, key=lambda item: item['order'])


def register_home(route, order=128):
    """Registers homepage elements"""
    global _home
    # TODO: currently supports only one plugin
    if _home is not None:
        raise Exception('Home is already registered')
    _home = route


@app.after_request
def shutdown_session(response):
    """Remove db_session after request"""
    db_session.remove()
    return response


def start(mg):
    """Start WEB UI"""

    global manager
    manager = mg

    # Create sqlachemy session for Flask usage
    global db_session
    db_session = scoped_session(sessionmaker(autocommit=False,
                                             autoflush=False,
                                             bind=manager.engine))
    if db_session is None:
        raise Exception('db_session is None')

    # Initialize manager
    manager.create_feeds()
    load_ui_plugins()

    # Daemonize after we load the ui plugins as they are loading from relative paths right now
    if os.name != 'nt' and manager.options.daemon:
        if threading.activeCount() != 1:
            log.critical('There are %r active threads. '
                         'Daemonizing now may cause strange failures.' % threading.enumerate())
        log.info('Creating FlexGet Daemon.')
        newpid = daemonize()
        # Write new pid to lock file
        log.debug('Writing new pid %d to lock file %s' % (newpid, manager.lockfile))
        lockfile = file(manager.lockfile, 'w')
        try:
            lockfile.write('%d\n' % newpid)
        finally:
            lockfile.close()

    # quick hack: since ui plugins may add tables to SQLAlchemy too and they're not initialized because create
    # was called when instantiating manager .. so we need to call it again
    from flexget.manager import Base
    Base.metadata.create_all(bind=manager.engine)

    fire_event('webui.start')

    # Start the executor thread
    global executor
    executor = ExecThread()
    executor.start()

    # Start Flask
    app.secret_key = os.urandom(24)
    """
    app.run(host='0.0.0.0', port=manager.options.port,
            use_reloader=manager.options.autoreload, debug=manager.options.debug)
    """

    set_exit_handler(stop_server)

    if manager.options.autoreload:
        # Create and destroy a socket so that any exceptions are raised before
        # we spawn a separate Python interpreter and lose this ability.
        import socket
        from werkzeug.serving import run_with_reloader
        reloader_interval = 1
        extra_files = None
        test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        test_socket.bind(('0.0.0.0', manager.options.port))
        test_socket.close()
        run_with_reloader(start_server, extra_files, reloader_interval)
    else:
        start_server()

    log.debug('server exited')
    fire_event('webui.stop')


def start_server():
    global server
    from cherrypy import wsgiserver
    d = wsgiserver.WSGIPathInfoDispatcher({'/': app})
    server = wsgiserver.CherryPyWSGIServer(('0.0.0.0', manager.options.port), d)

    log.debug('server %s' % server)
    try:
        server.start()
    except KeyboardInterrupt:
        stop_server()


def stop_server(*args):
    log.debug('Shutting down server')
    if server:
        server.stop()


def set_exit_handler(func):
    """Sets a callback function for term signal on windows or linux"""
    if os.name == 'nt':
        try:
            import win32api
            win32api.SetConsoleCtrlHandler(func, True)
        except ImportError:
            version = '.'.join(map(str, sys.version_info[:2]))
            raise Exception('pywin32 not installed for Python ' + version)
    else:
        import signal
        signal.signal(signal.SIGTERM, func)


def daemonize(stdin='/dev/null', stdout='/dev/null', stderr='/dev/null'):
    """Daemonizes the current process. Returns the new pid"""
    import atexit

    try:
        pid = os.fork()
        if pid > 0:
            # Don't run the exit handlers on the parent
            atexit._exithandlers = []
            # exit first parent
            sys.exit(0)
    except OSError, e:
        sys.stderr.write("fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
        sys.exit(1)

    # decouple from parent environment
    os.chdir('/')
    os.setsid()
    os.umask(0)

    # do second fork
    try:
        pid = os.fork()
        if pid > 0:
            # Don't run the exit handlers on the parent
            atexit._exithandlers = []
            # exit from second parent
            sys.exit(0)
    except OSError, e:
        sys.stderr.write("fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
        sys.exit(1)

    # redirect standard file descriptors
    sys.stdout.flush()
    sys.stderr.flush()
    si = file(stdin, 'r')
    so = file(stdout, 'a+')
    se = file(stderr, 'a+', 0)
    os.dup2(si.fileno(), sys.stdin.fileno())
    os.dup2(so.fileno(), sys.stdout.fileno())
    os.dup2(se.fileno(), sys.stderr.fileno())

    return os.getpid()