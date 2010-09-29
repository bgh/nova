# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Cloud Controller: Implementation of EC2 REST API calls, which are
dispatched to other nodes via AMQP RPC. State is via distributed
datastore.
"""

import base64
import datetime
import logging
import os
import time

from nova import crypto
from nova import db
from nova import exception
from nova import flags
from nova import quota
from nova import rpc
from nova import utils
from nova.compute.instance_types import INSTANCE_TYPES
from nova.api.ec2 import images


FLAGS = flags.FLAGS
flags.DECLARE('storage_availability_zone', 'nova.volume.manager')


class QuotaError(exception.ApiError):
    """Quota Exceeeded"""
    pass


def _gen_key(context, user_id, key_name):
    """Generate a key

    This is a module level method because it is slow and we need to defer
    it into a process pool."""
    # NOTE(vish): generating key pair is slow so check for legal
    #             creation before creating key_pair
    try:
        db.key_pair_get(context, user_id, key_name)
        raise exception.Duplicate("The key_pair %s already exists"
                                  % key_name)
    except exception.NotFound:
        pass
    private_key, public_key, fingerprint = crypto.generate_key_pair()
    key = {}
    key['user_id'] = user_id
    key['name'] = key_name
    key['public_key'] = public_key
    key['fingerprint'] = fingerprint
    db.key_pair_create(context, key)
    return {'private_key': private_key, 'fingerprint': fingerprint}


class CloudController(object):
    """ CloudController provides the critical dispatch between
 inbound API calls through the endpoint and messages
 sent to the other nodes.
"""
    def __init__(self):
        self.network_manager = utils.import_object(FLAGS.network_manager)
        self.setup()

    def __str__(self):
        return 'CloudController'

    def setup(self):
        """ Ensure the keychains and folders exist. """
        # FIXME(ja): this should be moved to a nova-manage command,
        # if not setup throw exceptions instead of running
        # Create keys folder, if it doesn't exist
        if not os.path.exists(FLAGS.keys_path):
            os.makedirs(FLAGS.keys_path)
        # Gen root CA, if we don't have one
        root_ca_path = os.path.join(FLAGS.ca_path, FLAGS.ca_file)
        if not os.path.exists(root_ca_path):
            start = os.getcwd()
            os.chdir(FLAGS.ca_path)
            # TODO(vish): Do this with M2Crypto instead
            utils.runthis("Generating root CA: %s", "sh genrootca.sh")
            os.chdir(start)

    def _get_mpi_data(self, project_id):
        result = {}
        for instance in db.instance_get_all_by_project(None, project_id):
            if instance['fixed_ip']:
                line = '%s slots=%d' % (instance['fixed_ip']['address'],
                    INSTANCE_TYPES[instance['instance_type']]['vcpus'])
                key = str(instance['key_name'])
                if key in result:
                    result[key].append(line)
                else:
                    result[key] = [line]
        return result

    def get_metadata(self, address):
        instance_ref = db.fixed_ip_get_instance(None, address)
        if instance_ref is None:
            return None
        mpi = self._get_mpi_data(instance_ref['project_id'])
        if instance_ref['key_name']:
            keys = {
                '0': {
                    '_name': instance_ref['key_name'],
                    'openssh-key': instance_ref['key_data']
                }
            }
        else:
            keys = ''
        hostname = instance_ref['hostname']
        floating_ip = db.instance_get_floating_address(None,
                                                       instance_ref['id'])
        data = {
            'user-data': base64.b64decode(instance_ref['user_data']),
            'meta-data': {
                'ami-id': instance_ref['image_id'],
                'ami-launch-index': instance_ref['launch_index'],
                'ami-manifest-path': 'FIXME',
                'block-device-mapping': {  # TODO(vish): replace with real data
                    'ami': 'sda1',
                    'ephemeral0': 'sda2',
                    'root': '/dev/sda1',
                    'swap': 'sda3'
                },
                'hostname': hostname,
                'instance-action': 'none',
                'instance-id': instance_ref['ec2_id'],
                'instance-type': instance_ref['instance_type'],
                'local-hostname': hostname,
                'local-ipv4': address,
                'kernel-id': instance_ref['kernel_id'],
                'placement': {
                    'availability-zone': 'nova' # TODO(vish): real zone
                },
                'public-hostname': hostname,
                'public-ipv4': floating_ip or '',
                'public-keys': keys,
                'ramdisk-id': instance_ref['ramdisk_id'],
                'reservation-id': instance_ref['reservation_id'],
                'security-groups': '',
                'mpi': mpi
            }
        }
        if False:  # TODO(vish): store ancestor ids
            data['ancestor-ami-ids'] = []
        if False:  # TODO(vish): store product codes
            data['product-codes'] = []
        return data

    def describe_availability_zones(self, context, **kwargs):
        return {'availabilityZoneInfo': [{'zoneName': 'nova',
                                          'zoneState': 'available'}]}

    def describe_regions(self, context, region_name=None, **kwargs):
        if FLAGS.region_list:
            regions = []
            for region in FLAGS.region_list:
                name, _sep, url = region.partition('=')
                regions.append({'regionName': name,
                                'regionEndpoint': url})
        else:
            regions = [{'regionName': 'nova',
                        'regionEndpoint': FLAGS.ec2_url}]
        if region_name:
            regions = [r for r in regions if r['regionName'] in region_name]
        return {'regionInfo': regions }

    def describe_snapshots(self,
                           context,
                           snapshot_id=None,
                           owner=None,
                           restorable_by=None,
                           **kwargs):
        return {'snapshotSet': [{'snapshotId': 'fixme',
                                 'volumeId': 'fixme',
                                 'status': 'fixme',
                                 'startTime': 'fixme',
                                 'progress': 'fixme',
                                 'ownerId': 'fixme',
                                 'volumeSize': 0,
                                 'description': 'fixme'}]}

    def describe_key_pairs(self, context, key_name=None, **kwargs):
        key_pairs = db.key_pair_get_all_by_user(context, context.user.id)
        if not key_name is None:
            key_pairs = [x for x in key_pairs if x['name'] in key_name]

        result = []
        for key_pair in key_pairs:
            # filter out the vpn keys
            suffix = FLAGS.vpn_key_suffix
            if context.user.is_admin() or not key_pair['name'].endswith(suffix):
                result.append({
                    'keyName': key_pair['name'],
                    'keyFingerprint': key_pair['fingerprint'],
                })

        return {'keypairsSet': result}

    def create_key_pair(self, context, key_name, **kwargs):
        data = _gen_key(None, context.user.id, key_name)
        return {'keyName': key_name,
                'keyFingerprint': data['fingerprint'],
                'keyMaterial': data['private_key']}
        # TODO(vish): when context is no longer an object, pass it here

    def delete_key_pair(self, context, key_name, **kwargs):
        try:
            db.key_pair_destroy(context, context.user.id, key_name)
        except exception.NotFound:
            # aws returns true even if the key doesn't exist
            pass
        return True

    def describe_security_groups(self, context, group_names, **kwargs):
        groups = {'securityGroupSet': []}

        # Stubbed for now to unblock other things.
        return groups

    def create_security_group(self, context, group_name, **kwargs):
        return True

    def delete_security_group(self, context, group_name, **kwargs):
        return True

    def get_console_output(self, context, instance_id, **kwargs):
        # instance_id is passed in as a list of instances
        instance_ref = db.instance_get_by_ec2_id(context, instance_id[0])
        return rpc.call('%s.%s' % (FLAGS.compute_topic,
                                   instance_ref['host']),
                        {"method": "get_console_output",
                         "args": {"context": None,
                                  "instance_id": instance_ref['id']}})

    def describe_volumes(self, context, **kwargs):
        if context.user.is_admin():
            volumes = db.volume_get_all(context)
        else:
            volumes = db.volume_get_all_by_project(context, context.project.id)

        volumes = [self._format_volume(context, v) for v in volumes]

        return {'volumeSet': volumes}

    def _format_volume(self, context, volume):
        v = {}
        v['volumeId'] = volume['ec2_id']
        v['status'] = volume['status']
        v['size'] = volume['size']
        v['availabilityZone'] = volume['availability_zone']
        v['createTime'] = volume['created_at']
        if context.user.is_admin():
            v['status'] = '%s (%s, %s, %s, %s)' % (
                volume['status'],
                volume['user_id'],
                volume['host'],
                volume['instance_id'],
                volume['mountpoint'])
        if volume['attach_status'] == 'attached':
            v['attachmentSet'] = [{'attachTime': volume['attach_time'],
                                   'deleteOnTermination': False,
                                   'device': volume['mountpoint'],
                                   'instanceId': volume['instance_id'],
                                   'status': 'attached',
                                   'volume_id': volume['ec2_id']}]
        else:
            v['attachmentSet'] = [{}]

        v['display_name'] = volume['display_name']
        v['display_description'] = volume['display_description']
        return v

    def create_volume(self, context, size, **kwargs):
        # check quota
        if quota.allowed_volumes(context, 1, size) < 1:
            logging.warn("Quota exceeeded for %s, tried to create %sG volume",
                         context.project.id, size)
            raise QuotaError("Volume quota exceeded. You cannot "
                             "create a volume of size %s" %
                             size)
        vol = {}
        vol['size'] = size
        vol['user_id'] = context.user.id
        vol['project_id'] = context.project.id
        vol['availability_zone'] = FLAGS.storage_availability_zone
        vol['status'] = "creating"
        vol['attach_status'] = "detached"
        vol['display_name'] = kwargs.get('display_name')
        vol['display_description'] = kwargs.get('display_description')
        volume_ref = db.volume_create(context, vol)

        rpc.cast(FLAGS.scheduler_topic,
                 {"method": "create_volume",
                  "args": {"context": None,
                           "topic": FLAGS.volume_topic,
                           "volume_id": volume_ref['id']}})

        return {'volumeSet': [self._format_volume(context, volume_ref)]}


    def attach_volume(self, context, volume_id, instance_id, device, **kwargs):
        volume_ref = db.volume_get_by_ec2_id(context, volume_id)
        # TODO(vish): abstract status checking?
        if volume_ref['status'] != "available":
            raise exception.ApiError("Volume status must be available")
        if volume_ref['attach_status'] == "attached":
            raise exception.ApiError("Volume is already attached")
        instance_ref = db.instance_get_by_ec2_id(context, instance_id)
        host = instance_ref['host']
        rpc.cast(db.queue_get_for(context, FLAGS.compute_topic, host),
                                {"method": "attach_volume",
                                 "args": {"context": None,
                                          "volume_id": volume_ref['id'],
                                          "instance_id": instance_ref['id'],
                                          "mountpoint": device}})
        return {'attachTime': volume_ref['attach_time'],
                'device': volume_ref['mountpoint'],
                'instanceId': instance_ref['id'],
                'requestId': context.request_id,
                'status': volume_ref['attach_status'],
                'volumeId': volume_ref['id']}

    def detach_volume(self, context, volume_id, **kwargs):
        volume_ref = db.volume_get_by_ec2_id(context, volume_id)
        instance_ref = db.volume_get_instance(context, volume_ref['id'])
        if not instance_ref:
            raise exception.ApiError("Volume isn't attached to anything!")
        # TODO(vish): abstract status checking?
        if volume_ref['status'] == "available":
            raise exception.ApiError("Volume is already detached")
        try:
            host = instance_ref['host']
            rpc.cast(db.queue_get_for(context, FLAGS.compute_topic, host),
                                {"method": "detach_volume",
                                 "args": {"context": None,
                                          "instance_id": instance_ref['id'],
                                          "volume_id": volume_ref['id']}})
        except exception.NotFound:
            # If the instance doesn't exist anymore,
            # then we need to call detach blind
            db.volume_detached(context)
        return {'attachTime': volume_ref['attach_time'],
                'device': volume_ref['mountpoint'],
                'instanceId': instance_ref['ec2_id'],
                'requestId': context.request_id,
                'status': volume_ref['attach_status'],
                'volumeId': volume_ref['id']}

    def _convert_to_set(self, lst, label):
        if lst == None or lst == []:
            return None
        if not isinstance(lst, list):
            lst = [lst]
        return [{label: x} for x in lst]

    def update_volume(self, context, volume_id, **kwargs):
        updatable_fields = ['display_name', 'display_description']
        changes = {}
        for field in updatable_fields:
            if field in kwargs:
                changes[field] = kwargs[field]
        if changes:
            db.volume_update(context, volume_id, kwargs)
        return True

    def describe_instances(self, context, **kwargs):
        return self._format_describe_instances(context)

    def _format_describe_instances(self, context):
        return { 'reservationSet': self._format_instances(context) }

    def _format_run_instances(self, context, reservation_id):
        i = self._format_instances(context, reservation_id)
        assert len(i) == 1
        return i[0]

    def _format_instances(self, context, reservation_id=None):
        reservations = {}
        if reservation_id:
            instances = db.instance_get_all_by_reservation(context,
                                                           reservation_id)
        else:
            if context.user.is_admin():
                instances = db.instance_get_all(context)
            else:
                instances = db.instance_get_all_by_project(context,
                                                           context.project.id)
        for instance in instances:
            if not context.user.is_admin():
                if instance['image_id'] == FLAGS.vpn_image_id:
                    continue
            i = {}
            i['instanceId'] = instance['ec2_id']
            i['imageId'] = instance['image_id']
            i['instanceState'] = {
                'code': instance['state'],
                'name': instance['state_description']
            }
            fixed_addr = None
            floating_addr = None
            if instance['fixed_ip']:
                fixed_addr = instance['fixed_ip']['address']
                if instance['fixed_ip']['floating_ips']:
                    fixed = instance['fixed_ip']
                    floating_addr = fixed['floating_ips'][0]['address']
            i['privateDnsName'] = fixed_addr
            i['publicDnsName'] = floating_addr
            i['dnsName'] = i['publicDnsName'] or i['privateDnsName']
            i['keyName'] = instance['key_name']
            if context.user.is_admin():
                i['keyName'] = '%s (%s, %s)' % (i['keyName'],
                    instance['project_id'],
                    instance['host'])
            i['productCodesSet'] = self._convert_to_set([], 'product_codes')
            i['instanceType'] = instance['instance_type']
            i['launchTime'] = instance['created_at']
            i['amiLaunchIndex'] = instance['launch_index']
            i['displayName'] = instance['display_name']
            i['displayDescription'] = instance['display_description']
            if not reservations.has_key(instance['reservation_id']):
                r = {}
                r['reservationId'] = instance['reservation_id']
                r['ownerId'] = instance['project_id']
                r['groupSet'] = self._convert_to_set([], 'groups')
                r['instancesSet'] = []
                reservations[instance['reservation_id']] = r
            reservations[instance['reservation_id']]['instancesSet'].append(i)

        return list(reservations.values())

    def describe_addresses(self, context, **kwargs):
        return self.format_addresses(context)

    def format_addresses(self, context):
        addresses = []
        if context.user.is_admin():
            iterator = db.floating_ip_get_all(context)
        else:
            iterator = db.floating_ip_get_all_by_project(context,
                                                         context.project.id)
        for floating_ip_ref in iterator:
            address = floating_ip_ref['address']
            instance_id = None
            if (floating_ip_ref['fixed_ip']
                and floating_ip_ref['fixed_ip']['instance']):
                instance_id = floating_ip_ref['fixed_ip']['instance']['ec2_id']
            address_rv = {'public_ip': address,
                          'instance_id': instance_id}
            if context.user.is_admin():
                details = "%s (%s)" % (address_rv['instance_id'],
                                       floating_ip_ref['project_id'])
                address_rv['instance_id'] = details
            addresses.append(address_rv)
        return {'addressesSet': addresses}

    def allocate_address(self, context, **kwargs):
        # check quota
        if quota.allowed_floating_ips(context, 1) < 1:
            logging.warn("Quota exceeeded for %s, tried to allocate address",
                         context.project.id)
            raise QuotaError("Address quota exceeded. You cannot "
                             "allocate any more addresses")
        network_topic = self._get_network_topic(context)
        public_ip = rpc.call(network_topic,
                         {"method": "allocate_floating_ip",
                          "args": {"context": None,
                                   "project_id": context.project.id}})
        return {'addressSet': [{'publicIp': public_ip}]}

    def release_address(self, context, public_ip, **kwargs):
        # NOTE(vish): Should we make sure this works?
        floating_ip_ref = db.floating_ip_get_by_address(context, public_ip)
        network_topic = self._get_network_topic(context)
        rpc.cast(network_topic,
                 {"method": "deallocate_floating_ip",
                  "args": {"context": None,
                           "floating_address": floating_ip_ref['address']}})
        return {'releaseResponse': ["Address released."]}

    def associate_address(self, context, instance_id, public_ip, **kwargs):
        instance_ref = db.instance_get_by_ec2_id(context, instance_id)
        fixed_address = db.instance_get_fixed_address(context,
                                                      instance_ref['id'])
        floating_ip_ref = db.floating_ip_get_by_address(context, public_ip)
        network_topic = self._get_network_topic(context)
        rpc.cast(network_topic,
                 {"method": "associate_floating_ip",
                  "args": {"context": None,
                           "floating_address": floating_ip_ref['address'],
                           "fixed_address": fixed_address}})
        return {'associateResponse': ["Address associated."]}

    def disassociate_address(self, context, public_ip, **kwargs):
        floating_ip_ref = db.floating_ip_get_by_address(context, public_ip)
        network_topic = self._get_network_topic(context)
        rpc.cast(network_topic,
                 {"method": "disassociate_floating_ip",
                  "args": {"context": None,
                           "floating_address": floating_ip_ref['address']}})
        return {'disassociateResponse': ["Address disassociated."]}

    def _get_network_topic(self, context):
        """Retrieves the network host for a project"""
        network_ref = db.project_get_network(context, context.project.id)
        host = network_ref['host']
        if not host:
            host = rpc.call(FLAGS.network_topic,
                                  {"method": "set_network_host",
                                   "args": {"context": None,
                                            "project_id": context.project.id}})
        return db.queue_get_for(context, FLAGS.network_topic, host)

    def run_instances(self, context, **kwargs):
        instance_type = kwargs.get('instance_type', 'm1.small')
        if instance_type not in INSTANCE_TYPES:
            raise exception.ApiError("Unknown instance type: %s",
                                     instance_type)
        # check quota
        max_instances = int(kwargs.get('max_count', 1))
        min_instances = int(kwargs.get('min_count', max_instances))
        num_instances = quota.allowed_instances(context,
                                                max_instances,
                                                instance_type)
        if num_instances < min_instances:
            logging.warn("Quota exceeeded for %s, tried to run %s instances",
                         context.project.id, min_instances)
            raise QuotaError("Instance quota exceeded. You can only "
                             "run %s more instances of this type." %
                             num_instances, "InstanceLimitExceeded")
        # make sure user can access the image
        # vpn image is private so it doesn't show up on lists
        vpn = kwargs['image_id'] == FLAGS.vpn_image_id

        if not vpn:
            image = images.get(context, kwargs['image_id'])

        # FIXME(ja): if image is vpn, this breaks
        # get defaults from imagestore
        image_id = image['imageId']
        kernel_id = image.get('kernelId', FLAGS.default_kernel)
        ramdisk_id = image.get('ramdiskId', FLAGS.default_ramdisk)

        # API parameters overrides of defaults
        kernel_id = kwargs.get('kernel_id', kernel_id)
        ramdisk_id = kwargs.get('ramdisk_id', ramdisk_id)

        # make sure we have access to kernel and ramdisk
        images.get(context, kernel_id)
        images.get(context, ramdisk_id)

        logging.debug("Going to run %s instances...", num_instances)
        launch_time = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        key_data = None
        if kwargs.has_key('key_name'):
            key_pair_ref = db.key_pair_get(context,
                                      context.user.id,
                                      kwargs['key_name'])
            key_data = key_pair_ref['public_key']

        # TODO: Get the real security group of launch in here
        security_group = "default"

        reservation_id = utils.generate_uid('r')
        base_options = {}
        base_options['state_description'] = 'scheduling'
        base_options['image_id'] = image_id
        base_options['kernel_id'] = kernel_id
        base_options['ramdisk_id'] = ramdisk_id
        base_options['reservation_id'] = reservation_id
        base_options['key_data'] = key_data
        base_options['key_name'] = kwargs.get('key_name', None)
        base_options['user_id'] = context.user.id
        base_options['project_id'] = context.project.id
        base_options['user_data'] = kwargs.get('user_data', '')
        base_options['security_group'] = security_group
        base_options['instance_type'] = instance_type
        base_options['display_name'] = kwargs.get('display_name')
        base_options['display_description'] = kwargs.get('display_description')

        type_data = INSTANCE_TYPES[instance_type]
        base_options['memory_mb'] = type_data['memory_mb']
        base_options['vcpus'] = type_data['vcpus']
        base_options['local_gb'] = type_data['local_gb']

        for num in range(num_instances):
            instance_ref = db.instance_create(context, base_options)
            inst_id = instance_ref['id']

            inst = {}
            inst['mac_address'] = utils.generate_mac()
            inst['launch_index'] = num
            inst['hostname'] = instance_ref['ec2_id']
            db.instance_update(context, inst_id, inst)
            address = self.network_manager.allocate_fixed_ip(context,
                                                             inst_id,
                                                             vpn)

            # TODO(vish): This probably should be done in the scheduler
            #             network is setup when host is assigned
            network_topic = self._get_network_topic(context)
            rpc.call(network_topic,
                     {"method": "setup_fixed_ip",
                      "args": {"context": None,
                               "address": address}})

            rpc.cast(FLAGS.scheduler_topic,
                     {"method": "run_instance",
                      "args": {"context": None,
                               "topic": FLAGS.compute_topic,
                               "instance_id": inst_id}})
            logging.debug("Casting to scheduler for %s/%s's instance %s" %
                      (context.project.name, context.user.name, inst_id))
        return self._format_run_instances(context, reservation_id)


    def terminate_instances(self, context, instance_id, **kwargs):
        logging.debug("Going to start terminating instances")
        for id_str in instance_id:
            logging.debug("Going to try and terminate %s" % id_str)
            try:
                instance_ref = db.instance_get_by_ec2_id(context, id_str)
            except exception.NotFound:
                logging.warning("Instance %s was not found during terminate"
                                % id_str)
                continue

            now = datetime.datetime.utcnow()
            db.instance_update(context,
                               instance_ref['id'],
                               {'terminated_at': now})
            # FIXME(ja): where should network deallocate occur?
            address = db.instance_get_floating_address(context,
                                                       instance_ref['id'])
            if address:
                logging.debug("Disassociating address %s" % address)
                # NOTE(vish): Right now we don't really care if the ip is
                #             disassociated.  We may need to worry about
                #             checking this later.  Perhaps in the scheduler?
                network_topic = self._get_network_topic(context)
                rpc.cast(network_topic,
                         {"method": "disassociate_floating_ip",
                          "args": {"context": None,
                                   "floating_address": address}})

            address = db.instance_get_fixed_address(context,
                                                    instance_ref['id'])
            if address:
                logging.debug("Deallocating address %s" % address)
                # NOTE(vish): Currently, nothing needs to be done on the
                #             network node until release. If this changes,
                #             we will need to cast here.
                self.network_manager.deallocate_fixed_ip(context, address)

            host = instance_ref['host']
            if host:
                rpc.cast(db.queue_get_for(context, FLAGS.compute_topic, host),
                         {"method": "terminate_instance",
                          "args": {"context": None,
                                   "instance_id": instance_ref['id']}})
            else:
                db.instance_destroy(context, instance_ref['id'])
        return True

    def reboot_instances(self, context, instance_id, **kwargs):
        """instance_id is a list of instance ids"""
        for id_str in instance_id:
            instance_ref = db.instance_get_by_ec2_id(context, id_str)
            host = instance_ref['host']
            rpc.cast(db.queue_get_for(context, FLAGS.compute_topic, host),
                     {"method": "reboot_instance",
                      "args": {"context": None,
                               "instance_id": instance_ref['id']}})
        return True

    def update_instance(self, context, instance_id, **kwargs):
        updatable_fields = ['display_name', 'display_description']
        changes = {}
        for field in updatable_fields:
            if field in kwargs:
                changes[field] = kwargs[field]
        if changes:
            db_context = {}
            inst = db.instance_get_by_ec2_id(db_context, instance_id)
            db.instance_update(db_context, inst['id'], kwargs)
        return True

    def delete_volume(self, context, volume_id, **kwargs):
        # TODO: return error if not authorized
        volume_ref = db.volume_get_by_ec2_id(context, volume_id)
        if volume_ref['status'] != "available":
            raise exception.ApiError("Volume status must be available")
        now = datetime.datetime.utcnow()
        db.volume_update(context, volume_ref['id'], {'terminated_at': now})
        host = volume_ref['host']
        rpc.cast(db.queue_get_for(context, FLAGS.volume_topic, host),
                            {"method": "delete_volume",
                             "args": {"context": None,
                                      "volume_id": volume_ref['id']}})
        return True

    def describe_images(self, context, image_id=None, **kwargs):
        # The objectstore does its own authorization for describe
        imageSet = images.list(context, image_id)
        return {'imagesSet': imageSet}

    def deregister_image(self, context, image_id, **kwargs):
        # FIXME: should the objectstore be doing these authorization checks?
        images.deregister(context, image_id)
        return {'imageId': image_id}

    def register_image(self, context, image_location=None, **kwargs):
        # FIXME: should the objectstore be doing these authorization checks?
        if image_location is None and kwargs.has_key('name'):
            image_location = kwargs['name']
        image_id = images.register(context, image_location)
        logging.debug("Registered %s as %s" % (image_location, image_id))
        return {'imageId': image_id}

    def describe_image_attribute(self, context, image_id, attribute, **kwargs):
        if attribute != 'launchPermission':
            raise exception.ApiError('attribute not supported: %s' % attribute)
        try:
            image = images.list(context, image_id)[0]
        except IndexError:
            raise exception.ApiError('invalid id: %s' % image_id)
        result = {'image_id': image_id, 'launchPermission': []}
        if image['isPublic']:
            result['launchPermission'].append({'group': 'all'})
        return result

    def modify_image_attribute(self, context, image_id, attribute, operation_type, **kwargs):
        # TODO(devcamcar): Support users and groups other than 'all'.
        if attribute != 'launchPermission':
            raise exception.ApiError('attribute not supported: %s' % attribute)
        if not 'user_group' in kwargs:
            raise exception.ApiError('user or group not specified')
        if len(kwargs['user_group']) != 1 and kwargs['user_group'][0] != 'all':
            raise exception.ApiError('only group "all" is supported')
        if not operation_type in ['add', 'remove']:
            raise exception.ApiError('operation_type must be add or remove')
        return images.modify(context, image_id, operation_type)

    def update_image(self, context, image_id, **kwargs):
        result = images.update(context, image_id, dict(kwargs))
        return result