#!/usr/bin/env python2

import subprocess
import argparse
import sys
import os
import json
import time
import re
import logging
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import text

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
)

from coprs import db, app, helpers, models
from coprs.logic.builds_logic import BuildsLogic
from coprs.logic.coprs_logic import CoprsLogic

logging.basicConfig(
    filename="/var/log/copr/check_for_anitya_version_updates.log",
    format='[%(asctime)s][%(levelname)6s]: %(message)s',
    level=logging.DEBUG)
log = logging.getLogger(__name__)

parser = argparse.ArgumentParser(description='Fetch package version updates by using datagrepper log of anitya emitted messages and issue rebuilds of the respective COPR packages for each such update. Requires httpie package.')

parser.add_argument('--backend', action='store', default='pypi',
                   help='only check for updates from backend BACKEND, default pypi')
parser.add_argument('--delta', action='store', type=int, metavar='SECONDS', default=86400,
                   help='ignore updates older than SECONDS, default 86400')
parser.add_argument('-v', '--version', action='version', version='1.0',
                   help='print program version and exit')

args = parser.parse_args()


def logdebug(msg):
    print msg
    log.debug(msg)

def loginfo(msg):
    print msg
    log.info(msg)

def logerror(msg):
    print >> sys.stderr, msg
    log.error(msg)

def logexception(msg):
    print >> sys.stderr, msg
    log.exception(msg)

def run_cmd(cmd):
    """
    Run given command in a subprocess
    """
    loginfo('Executing: '+' '.join(cmd))
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (stdout, stderr) = process.communicate()
    if process.returncode != 0:
        logerror(stderr)
        sys.exit(1)
    return stdout

def to_json(data):
    try:
        data_json = json.loads(data)
    except Exception as e:
        loginfo(data)
        logexception(str(e))
    return data_json

def get_updates_messages():
    get_updates_cmd = ['http', 'get', 'https://apps.fedoraproject.org/datagrepper/raw', 'category==anitya', 'delta=={0}'.format(args.delta), 'topic==org.release-monitoring.prod.anitya.project.version.update', 'rows_per_page==64', 'order==asc']
    get_updates_cmd_paged = get_updates_cmd + ['page==1']
    result_json = to_json(run_cmd(get_updates_cmd_paged))
    messages = result_json['raw_messages']
    pages = result_json['pages']

    for p in range(2, pages+1):
        get_updates_cmd_paged = get_updates_cmd + ['page=='+str(p)]
        result_json = to_json(run_cmd(get_updates_cmd_paged))
        messages += result_json['raw_messages']

    return messages

def get_updated_packages(updates_messages):
    updated_packages = {}
    for message in updates_messages:
        update = message['msg']
        project = update['project']
        if args.backend.lower() != project['backend'].lower():
            continue
        updated_packages[project['name'].lower()] = project['version']
    return updated_packages

def get_copr_package_info_rows():
    source_type = helpers.BuildSourceEnum(args.backend.lower())
    if db.engine.url.drivername == "sqlite":
        placeholder = '?'
        true = '1'
    else:
        placeholder = '%s'
        true = 'true'
    rows = db.engine.execute(
        """
        SELECT package.id AS package_id, package.source_json AS source_json, build.pkg_version AS pkg_version, package.copr_id AS copr_id
        FROM package
        LEFT OUTER JOIN build ON build.package_id = package.id
        WHERE package.source_type = {placeholder} AND
              package.webhook_rebuild = {true} AND
              (build.id is NULL OR build.id = (SELECT MAX(build.id) FROM build WHERE build.package_id = package.id));
        """.format(placeholder=placeholder, true=true), source_type
    )
    return rows

def main():
    updated_packages = get_updated_packages(get_updates_messages())
    loginfo('Updated packages according to datagrepper: {0}'.format(updated_packages))

    for row in get_copr_package_info_rows():
        source_json = json.loads(row.source_json)
        source_package_name = source_json['pypi_package_name'].lower()
        source_python_version = source_json['python_version']
        latest_build_version = row.pkg_version
        logdebug('candidate package for rebuild: {0}'.format(source_package_name))
        if source_package_name in updated_packages:
            new_updated_version = updated_packages[source_package_name]
            logdebug('source_package_name: {0}, latest_build_version: {1}, new_updated_version {2}'.format(source_package_name, latest_build_version, new_updated_version))
            if not latest_build_version or not re.match(new_updated_version, latest_build_version): # if the last build's package version is "different" from new remote package version, rebuild
                copr = CoprsLogic.get_by_id(row.copr_id)[0]
                if args.backend.lower() == 'pypi':
                    loginfo('Launching pypi build for package of source name: {0}, package id: {1}, copr id: {2}, user id: {3}'.format(source_package_name, row.package_id, copr.id, copr.owner.id))
                    build = BuildsLogic.create_new_from_pypi(copr.owner, copr, source_package_name, new_updated_version, source_python_version, chroot_names=None)
                else:
                    raise Exception('Unsupported backend {0} passed as command-line argument'.format(args.backend))
                db.session.commit()
                loginfo('Launched build id {0}'.format(build.id))

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logexception(str(e))
