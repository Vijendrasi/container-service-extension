# container-service-extension
# Copyright (c) 2017 VMware, Inc. All Rights Reserved.
# SPDX-License-Identifier: BSD-2-Clause

import hashlib
import json
import os
import pathlib
import stat
import sys
import traceback
from urllib.parse import urlparse

import click
import requests
from cachetools import LRUCache
from container_service_extension.exceptions import VcdResponseError
from lxml import objectify
from pyvcloud.vcd.client import BasicLoginCredentials
from pyvcloud.vcd.client import Client
from pyvcloud.vcd.exceptions import EntityNotFoundException
from pyvcloud.vcd.org import Org
from pyvcloud.vcd.platform import Platform
from pyvcloud.vcd.vapp import VApp
from pyvcloud.vcd.vdc import VDC
from pyvcloud.vcd.vm import VM
from vsphere_guest_run.vsphere import VSphere

cache = LRUCache(maxsize=1024)
SYSTEM_ORG_NAME = "System"
CSE_SCRIPTS_DIR = 'container_service_extension_scripts'
ERROR_REASON = "reason"
ERROR_DESCRIPTION = "description"
ERROR_STACKTRACE = "stacktrace"
ERROR_MESSAGE = "message"
ERROR_UNKNOWN = "unknown error"

# used to set up and start AMQP exchange
EXCHANGE_TYPE = 'direct'

# chunk size in bytes for file reading
BUF_SIZE = 65536

# chunk size for downloading files
SIZE_1MB = 1024 * 1024

_type_to_string = {
    str: 'string',
    int: 'number',
    bool: 'true/false',
    dict: 'mapping',
    list: 'sequence',
}


def error_to_json(error):
    """converts the given python exception object to dictionary
    with attributes short reason, long description and stacktrace of the error.

    :param error: Exception object.

    :return: dictionary with error reason, error description and stacktrace; or empty dictionary

    :rtype: dict
    """
    if error:
        error_string = str(error)
        reasons = error_string.split(',')
        return {
            ERROR_MESSAGE: {
                ERROR_REASON: reasons[0],
                ERROR_DESCRIPTION: error_string,
                ERROR_STACKTRACE: traceback.format_exception(error.__class__, error, error.__traceback__)
            }
        }
    return dict()


def process_response(response):
    """Process the given response dictionary with following keys.
    status_code: http status code
    content: response result as string

    Returns the response content, if the value of status code property is 2xx
    Otherwise raises exception with error message

    :param requests.models.Response response: object with attributes status code and content

    :return: decoded response content if status code is 2xx.

    :rtype: dict

    :raises VcdResponseError: if response http status code is not 2xx
    """
    if response.status_code in [
        requests.codes.ok, requests.codes.created,
        requests.codes.accepted
    ]:
        return deserialize_response_content(response)
    else:
        response_to_exception(response)


def deserialize_response_content(response):
    """Since the response is encoded in utf-8, it gets decoded to
    regular python string that will be in json string. That gets converted to
    python dictionary.

    Note: Do not use this method to process non-json response.content

    :param requests.models.Response response: object that includes attributes status code and content

    :return: response content as decoded dictionary

    :rtype: dict
    """
    decoded = response.content.decode("utf-8")
    if len(decoded) > 0:
        return json.loads(decoded)
    else:
        return dict()


def response_to_exception(response):
    """Raises exception with appropriate messages, depending on the key: status code

    :param requests.models.Response response: object that has attributes status code and content

    :raises: VcdResponseError
    """
    if response.status_code == 504:
        message = 'An error has occurred.'
        if response.content is not None and len(response.content) > 0:
            obj = objectify.fromstring(response.content)
            message = obj.get(ERROR_MESSAGE)
        raise VcdResponseError(response.status_code, message)

    content = deserialize_response_content(response)
    if ERROR_MESSAGE in content:
        if ERROR_REASON in content[ERROR_MESSAGE]:
            message = content[ERROR_MESSAGE][ERROR_REASON]
        else:
            message = content[ERROR_MESSAGE]
    else:
        message = ERROR_UNKNOWN

    raise VcdResponseError(response.status_code, message)


def bool_to_msg(value):
    if value:
        return 'success'
    return 'fail'


def get_sha256(filepath):
    """Gets sha256 hash of file as a string.

    :param str filepath: path to file.

    :return: sha256 string for the file.

    :rtype: str
    """
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while True:
            data = f.read(BUF_SIZE)
            if not data:
                break
            sha256.update(data)
    return sha256.hexdigest()


def check_keys_and_value_types(dikt, ref_dict, location='dictionary'):
    """Compares a dictionary with a reference dictionary to ensure that
    all keys and value types are the same.

    :param dict dikt: the dictionary to check for validity
    :param dict ref_dict: the dictionary to check against
    :param str location: where this check is taking place, so error messages
        can be more descriptive.

    :raises KeyError: if @dikt has missing or invalid keys
    :raises ValueError: if the value of a property in @dikt does not match with
        the value of the same property in @ref_dict
    """
    ref_keys = set(ref_dict.keys())
    keys = set(dikt.keys())

    missing_keys = ref_keys - keys
    invalid_keys = keys - ref_keys

    if missing_keys:
        click.secho(f"Missing keys in {location}: {missing_keys}", fg='red')
    if invalid_keys:
        click.secho(f"Invalid keys in {location}: {invalid_keys}", fg='red')

    bad_value = False
    for k in ref_keys:
        if k not in keys:
            continue
        value_type = type(ref_dict[k])
        if not isinstance(dikt[k], value_type):
            click.secho(f"{location} key '{k}': value type should be "
                        f"'{_type_to_string[value_type]}'", fg='red')
            bad_value = True

    if missing_keys or invalid_keys:
        raise KeyError(f"Missing and/or invalid key in {location}")
    if bad_value:
        raise ValueError(f"Incorrect type for property value(s) in {location}")


def check_python_version():
    """Ensures that user's Python version >= 3.6.

    :raises Exception: if user's Python version < 3.6.
    """
    major = sys.version_info.major
    minor = sys.version_info.minor
    click.echo(f"Required Python version: >= 3.6\nInstalled Python version: "
               f"{major}.{minor}.{sys.version_info.micro}")
    if major < 3 or (major == 3 and minor < 6):
        raise Exception("Python version should be 3.6 or greater")


def check_file_permissions(filename):
    """Ensures that the file only has rw permission for Owner, and no
    permissions for anyone else.

    :param str filename: path to file.

    :raises Exception: if file has 'x' permissions for Owner or 'rwx'
        permissions for 'Others' or 'Group'.
    """
    err_msgs = []
    file_mode = os.stat(filename).st_mode
    if file_mode & stat.S_IXUSR:
        msg = f"Remove execute permission of the Owner for the file {filename}"
        click.secho(msg, fg='red')
        err_msgs.append(msg)
    if file_mode & stat.S_IROTH or file_mode & stat.S_IWOTH \
            or file_mode & stat.S_IXOTH:
        msg = f"Remove read, write and execute permissions of Others for " \
              f"the file {filename}"
        click.secho(msg, fg='red')
        err_msgs.append(msg)
    if file_mode & stat.S_IRGRP or file_mode & stat.S_IWGRP \
            or file_mode & stat.S_IXGRP:
        msg = f"Remove read, write and execute permissions of Group for the " \
              f"file {filename}"
        click.secho(msg, fg='red')
        err_msgs.append(msg)

    if err_msgs:
        raise IOError(err_msgs)


def download_file(url, filepath, sha256=None, quiet=False, logger=None):
    """Downloads a file from a url to local filepath.

    Will not overwrite files unless @sha256 is given.
    Recursively creates specified directories in @filepath.

    :param str url: source url.
    :param str filepath: destination filepath.
    :param str sha256: without this argument, if a file already exists at
        @filepath, download will be skipped. If @sha256 matches the file's
        sha256, download will be skipped.
    :param bool quiet: If True, console output is disabled.
    :param logging.Logger logger: optional logger to log with.
    """
    path = pathlib.Path(filepath)
    if path.is_file() and (sha256 is None or get_sha256(filepath) == sha256):
        msg = f"Skipping download to '{filepath}' (file already exists)"
        if logger:
            logger.info(msg)
        if not quiet:
            click.secho(msg, fg='green')
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    msg = f"Downloading file from '{url}' to '{filepath}'..."
    if logger:
        logger.info(msg)
    if not quiet:
        click.secho(msg, fg='yellow')
    response = requests.get(url, stream=True)
    with path.open(mode='wb') as f:
        for chunk in response.iter_content(chunk_size=SIZE_1MB):
            f.write(chunk)
    msg = f"Download complete"
    if logger:
        logger.info(msg)
    if not quiet:
        click.secho(msg, fg='green')


def catalog_exists(org, catalog_name):
    """Boolean function to check if catalog exists.

    :param pyvcloud.vcd.org.Org org:
    :param str catalog_name:

    :return: True if catalog exists, False otherwise.

    :rtype: bool
    """
    try:
        org.get_catalog(catalog_name)
        return True
    except EntityNotFoundException:
        return False


def catalog_item_exists(org, catalog_name, catalog_item_name):
    """Boolean function to check if catalog item exists (name check).

    :param pyvcloud.vcd.org.Org org:
    :param str catalog_name:
    :param str catalog_item_name:

    :return: True if catalog item exists, False otherwise.

    :rtype: bool
    """
    try:
        org.get_catalog_item(catalog_name, catalog_item_name)
        return True
    except EntityNotFoundException:
        return False


def upload_ova_to_catalog(client, catalog_name, filepath, update=False,
                          org=None, org_name=None, logger=None):
    """Uploads local ova file to vCD catalog.

    :param pyvcloud.vcd.client.Client client:
    :param str filepath: file path to the .ova file.
    :param str catalog_name: name of catalog.
    :param bool update: signals whether to overwrite an existing catalog
        item with this new one.
    :param pyvcloud.vcd.org.Org org: specific org to use.
    :param str org_name: specific org to use if @org is not given.
        If None, uses currently logged-in org from @client.
    :param logging.Logger logger: optional logger to log with.


    :raises pyvcloud.vcd.exceptions.EntityNotFoundException if catalog
        does not exist.
    :raises pyvcloud.vcd.exceptions.UploadException if upload fails.
    """
    if org is None:
        org = get_org(client, org_name=org_name)
    catalog_item_name = pathlib.Path(filepath).name
    if update:
        try:
            msg = f"Update flag set. Checking catalog '{catalog_name}' for " \
                  f"'{catalog_item_name}'"
            click.secho(msg, fg='yellow')
            if logger:
                logger.info(msg)
            org.delete_catalog_item(catalog_name, catalog_item_name)
            org.reload()
            wait_for_catalog_item_to_resolve(client, catalog_name,
                                             catalog_item_name, org=org)
            msg = f"Update flag set. Checking catalog '{catalog_name}' for " \
                  f"'{catalog_item_name}'"
            click.secho(msg, fg='yellow')
            if logger:
                logger.info(msg)
        except EntityNotFoundException:
            pass
    else:
        try:
            org.get_catalog_item(catalog_name, catalog_item_name)
            msg = f"'{catalog_item_name}' already exists in catalog " \
                  f"'{catalog_name}'"
            click.secho(msg, fg='green')
            if logger:
                logger.info(msg)

            return
        except EntityNotFoundException:
            pass

    msg = f"Uploading '{catalog_item_name}' to catalog '{catalog_name}'"
    click.secho(msg, fg='yellow')
    if logger:
        logger.info(msg)
    org.upload_ovf(catalog_name, filepath)
    org.reload()
    wait_for_catalog_item_to_resolve(client, catalog_name, catalog_item_name,
                                     org=org)
    msg = f"Uploaded '{catalog_item_name}' to catalog '{catalog_name}'"
    click.secho(msg, fg='green')
    if logger:
        logger.info(msg)


def wait_for_catalog_item_to_resolve(client, catalog_name, catalog_item_name,
                                     org=None, org_name=None):
    """Waits for catalog item's most recent task to resolve.

    :param pyvcloud.vcd.client.Client client:
    :param str catalog_name:
    :param str catalog_item_name:
    :param pyvcloud.vcd.org.Org org: specific org to use.
    :param str org_name: specific org to use if @org is not given.
        If None, uses currently logged-in org from @client.

    :raises EntityNotFoundException: if the org or catalog or catalog item
        could not be found.
    """
    if org is None:
        org = get_org(client, org_name=org_name)
    item = org.get_catalog_item(catalog_name, catalog_item_name)
    resource = client.get_resource(item.Entity.get('href'))
    client.get_task_monitor().wait_for_success(resource.Tasks.Task[0])


def get_org(client, org_name=None):
    """Gets the specified or currently logged-in Org object.

    :param pyvcloud.vcd.client.Client client:
    :param str org_name: which org to use. If None, uses currently logged-in
        org from @client.

    :return: pyvcloud Org object

    :rtype: pyvcloud.vcd.org.Org

    :raises EntityNotFoundException: if the org could not be found.
    """
    if org_name is None:
        org_sparse_resource = client.get_org()
        org = Org(client, href=org_sparse_resource.get('href'))
    else:
        org = Org(client, resource=client.get_org_by_name(org_name))
    return org


def get_vdc(client, vdc_name, org=None, org_name=None):
    """Gets the specified VDC object.

    :param pyvcloud.vcd.client.Client client:
    :param str vdc_name:
    :param pyvcloud.vcd.org.Org org: specific org to use.
    :param str org_name: specific org to use if @org is not given.
        If None, uses currently logged-in org from @client.

    :return: pyvcloud VDC object

    :rtype: pyvcloud.vcd.vdc.VDC

    :raises EntityNotFoundException: if the vdc could not be found.
    """
    if org is None:
        org = get_org(client, org_name=org_name)
    vdc = VDC(client, resource=org.get_vdc(vdc_name))
    return vdc


def get_data_file(filename, logger=None):
    """Retrieves CSE script file content as a string.

    Used to retrieve builtin script files that users have installed
    via pip install or setup.py. Looks inside virtualenv site-packages, cwd,
    user/global site-packages, python libs, usr bins/Cellars, as well
    as any subdirectories in these paths named 'scripts' or
    'container_service_extension_scripts'.

    :param str filename: name of file (script) we want to get.
    :param logging.Logger logger: optional logger to log with.

    :return: the file contents as a string.

    :rtype: str

    :raises FileNotFoundError: if requested data file cannot be
        found.
    """
    # look in cwd first
    base_paths = [str(pathlib.Path())] + sys.path
    path = None
    for base_path in base_paths:
        possible_paths = [
            pathlib.Path(f"{base_path}/{CSE_SCRIPTS_DIR}/{filename}"),
            pathlib.Path(f"{base_path}/{filename}"),
            pathlib.Path(f"{base_path}/scripts/{filename}")
        ]
        for p in possible_paths:
            if p.is_file():
                path = p
                break
        if path is not None:
            break

    if path is None:
        msg = f"Requested data file '{filename}' not found"
        click.secho(msg, fg='red')
        if logger:
            logger.error(msg, exc_info=True)
        raise FileNotFoundError(msg)

    msg = f"Found data file: {path}"
    click.secho(msg, fg='green')
    if logger:
        logger.info(msg)
    return path.read_text()


def create_and_share_catalog(org, catalog_name, catalog_desc='', logger=None):
    """Creates and shares specified catalog.

    If catalog does not exist in vCD, create it. Share the specified catalog
    to all orgs.

    :param pyvcloud.vcd.org.Org org:
    :param str catalog_name:
    :param str catalog_desc:
    :param logging.Logger logger: optional logger to log with.

    :return: XML representation of specified catalog.

    :rtype: lxml.objectify.ObjectifiedElement

    :raises pyvcloud.vcd.exceptions.EntityNotFoundException: if catalog sharing
        fails due to catalog creation failing.
    """
    if catalog_exists(org, catalog_name):
        msg = f"Found catalog '{catalog_name}'"
        click.secho(msg, fg='green')
        if logger:
            logger.info(msg)
    else:
        msg = f"Creating catalog '{catalog_name}'"
        click.secho(msg, fg='yellow')
        if logger:
            logger.info(msg)
        org.create_catalog(catalog_name, catalog_desc)
        msg = f"Created catalog '{catalog_name}'"
        click.secho(msg, fg='green')
        if logger:
            logger.info(msg)
        org.reload()
    org.share_catalog(catalog_name)
    org.reload()
    return org.get_catalog(catalog_name)


def get_vsphere(config, vapp, vm_name, logger=None):
    """Get the VSphere object for a specific VM inside a VApp.

    :param dict config: CSE config as a dictionary
    :param pyvcloud.vcd.vapp.VApp vapp: VApp used to get the VM ID.
    :param str vm_name:
    :param logging.Logger logger: optional logger to log with.

    :return: VSphere object for a specific VM inside a VApp

    :rtype: vsphere_guest_run.vsphere.VSphere
    """
    global cache

    # get vm id from vm resource
    vm_id = vapp.get_vm(vm_name).get('id')
    if vm_id not in cache:
        client = Client(uri=config['vcd']['host'],
                        api_version=config['vcd']['api_version'],
                        verify_ssl_certs=config['vcd']['verify'],
                        log_headers=True,
                        log_bodies=True)
        credentials = BasicLoginCredentials(config['vcd']['username'],
                                            SYSTEM_ORG_NAME,
                                            config['vcd']['password'])
        client.set_credentials(credentials)

        # must recreate vapp, or cluster creation fails
        vapp = VApp(client, href=vapp.href)
        vm_resource = vapp.get_vm(vm_name)
        vm_sys = VM(client, resource=vm_resource)
        vcenter_name = vm_sys.get_vc()
        platform = Platform(client)
        vcenter = platform.get_vcenter(vcenter_name)
        vcenter_url = urlparse(vcenter.Url.text)
        cache_item = {
            'hostname': vcenter_url.hostname,
            'port': vcenter_url.port
        }
        for vc in config['vcs']:
            if vc['name'] == vcenter_name:
                cache_item['username'] = vc['username']
                cache_item['password'] = vc['password']
                break
        cache[vm_id] = cache_item

    if logger:
        logger.debug(f"VM ID: {vm_id}, Hostname: {cache[vm_id]['hostname']}")

    return VSphere(cache[vm_id]['hostname'], cache[vm_id]['username'],
                   cache[vm_id]['password'], cache[vm_id]['port'])


def vgr_callback(prepend_msg='', logger=None):
    """Creates a callback function to use for vsphere-guest-run functions.

    :param str prepend_msg: string to prepend to all messages received from
        vsphere-guest-run function.
    :param logging.Logger logger: logger to use in case of error.

    :return: callback function to print messages received
        from vsphere-guest-run

    :rtype: function
    """

    def callback(message, exception=None):
        msg = f"{prepend_msg}{message}"
        click.echo(msg)
        if logger:
            logger.info(msg)
        if exception is not None:
            click.secho(f"vsphere-guest-run error: {exception}", fg='red')
            if logger:
                logger.error("vsphere-guest-run error", exc_info=True)
    return callback


def wait_until_tools_ready(vapp, vsphere, callback=vgr_callback()):
    """Blocking function to ensure that a VSphere has VMware Tools ready.

    :param pyvcloud.vcd.vapp.VApp vapp:
    :param vsphere_guest_run.vsphere.VSphere vsphere:
    :param function callback: a function to print out messages received from
        vsphere-guest-run functions. Function signature should be like this:
        def callback(message, exception=None), where parameter 'message'
        is a string.
    """
    vsphere.connect()
    moid = vapp.get_vm_moid(vapp.name)
    vm = vsphere.get_vm_by_moid(moid)
    vsphere.wait_until_tools_ready(vm, sleep=5, callback=callback)
