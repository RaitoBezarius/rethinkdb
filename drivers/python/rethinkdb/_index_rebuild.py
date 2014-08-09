#!/usr/bin/env python
from __future__ import print_function

import sys, os, datetime, time, shutil, tempfile, subprocess, curses, random
from optparse import OptionParser
from ._backup import *

curses.setupterm()

info = "'rethinkdb index-rebuild' recreates outdated secondary indexes in a cluster.\n" + \
       "  This should be used after upgrading to a newer version of rethinkdb.  There\n" + \
       "  will be a notification in the web UI if any secondary indexes are out-of-date."
usage = "rethinkdb index-rebuild [-c HOST:PORT] [-a AUTH_KEY] [-n NUM] [-r (DB | DB.TABLE)]..."

# Prefix used for indexes that are being rebuilt
temp_index_prefix = '$reql_temp_index$_'

def print_restore_help():
    print(info)
    print(usage)
    print("")
    print("  FILE                             the archive file to restore data from")
    print("  -h [ --help ]                    print this help")
    print("  -c [ --connect ] HOST:PORT       host and client port of a rethinkdb node to connect")
    print("                                   to (defaults to localhost:28015)")
    print("  -a [ --auth ] AUTH_KEY           authorization key for rethinkdb clients")
    print("  -r [ --rebuild ] (DB | DB.TABLE) the databases or tables to rebuild indexes on")
    print("                                   (defaults to all databases and tables)")
    print("  -n NUM                           the number of concurrent indexes to rebuild")
    print("                                   (defaults to 1)")
    print("")
    print("EXAMPLES:")
    print("rethinkdb index-rebuild -c mnemosyne:39500")
    print("  rebuild all outdated secondary indexes from the cluster on the host 'mnemosyne',")
    print("  one at a time")
    print("")
    print("rethinkdb index-rebuild -r test -r production.users -n 5")
    print("  rebuild all outdated secondary indexes from a local cluster on all tables in the")
    print("  'test' database as well as the 'production.users' table, five at a time")

def parse_options():
    parser = OptionParser(add_help_option=False, usage=usage)
    parser.add_option("-c", "--connect", dest="host", metavar="HOST:PORT", default="localhost:28015", type="string")
    parser.add_option("-a", "--auth", dest="auth_key", metavar="KEY", default="", type="string")
    parser.add_option("-r", "--rebuild", dest="tables", metavar="DB | DB.TABLE", default=[], action="append", type="string")
    parser.add_option("-n", dest="concurrent", metavar="NUM", default=1, type="int")
    parser.add_option("--debug", dest="debug", default=False, action="store_true")
    parser.add_option("-h", "--help", dest="help", default=False, action="store_true")
    (options, args) = parser.parse_args()

    if options.help:
        print_restore_help()
        exit(0)

    # Check validity of arguments
    if len(args) != 0:
        raise RuntimeError("Error: No positional arguments supported")

    res = { }

    # Verify valid host:port --connect option
    (res["host"], res["port"]) = parse_connect_option(options.host)

    # Verify valid --import options
    res["tables"] = parse_db_table_options(options.tables)

    res["auth_key"] = options.auth_key
    res["concurrent"] = options.concurrent
    res["debug"] = options.debug
    return res

def print_progress(ratio, indexes):
    total_width = 40
    done_width = int(ratio * total_width)
    equals = "=" * done_width
    spaces = " " * (total_width - done_width)
    percent = int(100 * ratio)
    clear_line = curses.tigetstr('el')

    # Only print the currently building indexes if we have the capability to clear the line
    # Otherwise we will end up with trash data at the end of the line in some cases
    if clear_line is None:
        print("\r[%s%s] %3d%%" % (equals, spaces, percent), end='')
    else:
        index_list = (" - %s" % ", ".join(indexes)) if len(indexes) > 0 else ""
        print("\r%s[%s%s] %3d%%%s" % (clear_line, equals, spaces, percent, index_list), end='')

    sys.stdout.flush()

def new_connection(conn_store, options):
    # Check if the connection is up
    try:
        r.expr(0).run(conn_store[0])
    except:
        conn_store[0] = r.connect(options["host"], options["port"])
    return conn_store[0]

def get_table_outdated_indexes(conn, db, table):
    return r.db(db).table(table).index_status().filter(lambda i: i['outdated'])['index'].run(conn)

def get_outdated_indexes(progress, conn, db_tables):
    res = [ ]
    if len(db_tables) == 0:
        dbs = r.db_list().run(conn)
        db_tables = [(db, None) for db in dbs]

    for db_table in db_tables:
        if db_table[1] is not None:
            table_list = [db_table[1]]
        else:
            table_list = r.db(db_table[0]).table_list().run(conn)
        for table in table_list:
            outdated_indexes = get_table_outdated_indexes(conn, db_table[0], table)

            for index in outdated_indexes:
                res.append({'db': db_table[0], 'table': table, 'name': index})
    return res

def drop_outdated_temp_indexes(progress, conn, indexes):
    indexes_to_drop = [i for i in indexes if i['name'].find(temp_index_prefix) == 0]
    for index in indexes_to_drop:
        r.db(index['db']).table(index['table']).index_drop(index['name']).run(conn)
        indexes.remove(index)

def create_temp_index(progress, conn, index):
    # If this index is already being rebuilt, don't try to recreate it
    extant_indexes = r.db(index['db']).table(index['table']).index_status().map(lambda i: i['index']).run(conn)
    if index['temp_name'] not in extant_indexes:
        index_fn = r.db(index['db']).table(index['table']).index_status(index['name']).nth(0)['function']
        res = r.db(index['db']).table(index['table']).index_create(index['temp_name'], index_fn).run(conn)

        if res['created'] != 1:
            raise RuntimeError("Error: failed to create `%s.%s` index `%s`." % \
                               (index['db'], index['table'], index['name']))

def get_index_progress(progress, conn, index):
    status = r.db(index['db']).table(index['table']).index_status(index['temp_name']).nth(0).run(conn)
    if status['ready']:
        return None
    else:
        return float(status['blocks_processed']) / status['blocks_total']

def rename_index(progress, conn, index):
    res = r.db(index['db']).table(index['table']).index_rename(index['temp_name'], index['name'], overwrite=True).run(conn)
    if res['renamed'] != 1:
        raise RuntimeError("Error: failed to overwrite `%s.%s` index `%s`." % \
                           (index['db'], index['table'], index['name']))

def rebuild_indexes(options):
    conn_store = [r.connect(options['host'], options['port'])]
    conn_fn = lambda: new_connection(conn_store, options)

    indexes_to_build = rdb_call_wrapper(conn_fn, "get outdated indexes", get_outdated_indexes, options["tables"])
    indexes_in_progress = [ ]

    # Drop any outdated indexes with the temp_index_prefix
    rdb_call_wrapper(conn_fn, "drop temporary outdated indexes", drop_outdated_temp_indexes, indexes_to_build)

    random.shuffle(indexes_to_build)
    total_indexes = len(indexes_to_build)
    indexes_completed = 0
    progress_ratio = 0.0
    highest_progress = 0.0

    print("Rebuilding %d indexes." % total_indexes)
    while len(indexes_to_build) > 0 or len(indexes_in_progress) > 0:
        # Make sure we're running the right number of concurrent index rebuilds
        while len(indexes_to_build) > 0 and len(indexes_in_progress) < options["concurrent"]:
            index = indexes_to_build.pop()
            index['temp_name'] = temp_index_prefix + index['name']
            index['progress'] = 0
            index['ready'] = False

            rdb_call_wrapper(conn_fn, "create `%s.%s` index `%s`" % (index['db'], index['table'], index['name']),
                             create_temp_index, index)
            indexes_in_progress.append(index)

        # Report progress
        highest_progress = max(highest_progress, progress_ratio)
        print_progress(highest_progress,
                       map(lambda i: '`%s.%s:%s`' % (i['db'], i['table'], i['name']), indexes_in_progress))

        # Check the status of indexes in progress
        progress_ratio = 0.0
        for index in indexes_in_progress:
            index_progress = rdb_call_wrapper(conn_fn, "progress `%s.%s` index `%s`" % (index['db'], index['table'], index['name']),
                                              get_index_progress, index)
            if index_progress is None:
                rdb_call_wrapper(conn_fn, "rename `%s.%s` index `%s`" % (index['db'], index['table'], index['name']),
                                 rename_index, index)
                index['ready'] = True
            else:
                progress_ratio += index_progress / total_indexes

        indexes_in_progress = [index for index in indexes_in_progress if not index['ready']]
        indexes_completed = total_indexes - len(indexes_to_build) - len(indexes_in_progress)
        progress_ratio += float(indexes_completed) / total_indexes

        if len(indexes_in_progress) == options['concurrent'] or \
           (len(indexes_in_progress) > 0 and len(indexes_to_build) == 0):
            # Short sleep to keep from killing the CPU
            time.sleep(0.1)

    # Get past the progress bar line
    print_progress(1.0, [])
    print("")

def main():
    try:
        options = parse_options()
    except RuntimeError as ex:
        print("Usage: %s" % usage, file=sys.stderr)
        print(ex, file=sys.stderr)
        return 1

    try:
        start_time = time.time()
        rebuild_indexes(options)
    except RuntimeError as ex:
        print(ex, file=sys.stderr)
        return 1
    print("  Done (%d seconds)" % (time.time() - start_time))
    return 0

if __name__ == "__main__":
    exit(main())
