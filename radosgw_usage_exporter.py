#!/usr/bin/python
# -*- coding: utf-8 -*-

import argparse
import json
import logging
import os
import time
import warnings
from collections import defaultdict, Counter

import requests
from awsauth import S3Auth
from prometheus_client import start_http_server
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY

logging.basicConfig(level=logging.DEBUG)
DEBUG = int(os.environ.get('DEBUG', '0'))
SKIP_DELETED_BUCKET = int(os.environ.get('SKIP_DELETED_BUCKET', '1'))
SKIP_DELETED_USER = int(os.environ.get('SKIP_DELETED_USER', '1'))


class RADOSGWCollector(object):
    """RADOSGWCollector gathers bucket level usage data for all buckets from
    the specified RADOSGW and presents it in a format suitable for pulling via
    a Prometheus server.

    NOTE: By default RADOSGW Servers do not gather usage data and it must be
    enabled by 'rgw enable usage log = true' in the appropriate section
    of ceph.conf see Ceph documentation for details """

    def __init__(self, host, admin_entry, access_key, secret_key, cluster_name, insecure):
        super(RADOSGWCollector, self).__init__()
        self.host = host
        self.access_key = access_key
        self.secret_key = secret_key
        self.cluster_name = cluster_name
        self.insecure = insecure

        # helpers for default schema
        if not self.host.startswith("http"):
            self.host = "http://{0}".format(self.host)
        # and for request_uri
        if not self.host.endswith("/"):
            self.host = "{0}/".format(self.host)

        self.url = "{0}{1}/".format(self.host, admin_entry)
        # Prepare Requests Session
        self._session()

    def collect(self):
        """
        * Collect 'usage' data:
            https://docs.ceph.com/en/latest/radosgw/adminops/#get-usage
        * Collect 'bucket' data:
            https://docs.ceph.com/docs/master/radosgw/adminops/#get-bucket-info
        """

        start = time.time()

        self._total_objects = 0
        self._total_bytes = 0
        self._total_bytes_sent = 0
        self._total_bytes_received = 0
        self._total_ops = 0
        self._total_successful_ops = 0
        self._ops = {}
        self._successful_ops = {}

        # setup empty prometheus metrics
        self._setup_empty_prometheus_metrics()

        # setup dict for aggregating bucket usage across "bins"
        self.usage_dict = defaultdict(dict)
        self.user_buckets = defaultdict(dict)
        rgw_usage = self._request_data(query='usage', args='show-summary=True')
        rgw_bucket = self._request_data(query='bucket', args='stats=True')
        rgw_users = self._get_rgw_users()

        # populate metrics with data
        if rgw_usage:
            for entry in rgw_usage['entries']:
                self._get_usage(entry)
            self._update_usage_metrics()

        if rgw_bucket:
            for bucket in rgw_bucket:
                self._get_bucket_usage(bucket)

        if rgw_users:
            if DEBUG:
                print('RGW users\n', rgw_users)
            for user in rgw_users:
                self._get_user_quota(user)
                self._get_user_info(user)

        # Update user summary
        if rgw_usage:
            for user_summary in rgw_usage['summary']:
                if not self._skip_user(user_summary, rgw_users):
                    self._update_usage_summary_metrics(user_summary)

        # Update bucket summary
        if rgw_usage:
            if DEBUG:
                print('RGW usage\n', (json.dumps(rgw_usage, indent=4, sort_keys=True)))
            for entry in rgw_usage['entries']:
                self._update_bucket_usage_summary_metrics(entry, rgw_users)

        duration = time.time() - start
        self._prometheus_metrics['scrape_duration_seconds'].add_metric(
            [], duration)

        if rgw_users is None:
            self._prometheus_metrics['total_users'].add_metric(
                [], 0)
        else:
            self._prometheus_metrics['total_users'].add_metric(
                [], len(rgw_users))

        if rgw_bucket is None:
            self._prometheus_metrics['total_buckets'].add_metric(
                [], 0)
        else:
            self._prometheus_metrics['total_buckets'].add_metric(
                [], len(rgw_bucket))

        self._prometheus_metrics['total_objects'].add_metric(
            [], self._total_objects)

        self._prometheus_metrics['total_bytes'].add_metric(
            [], self._total_bytes)

        self._prometheus_metrics['total_bytes_sent'].add_metric(
            [], self._total_bytes_sent)

        self._prometheus_metrics['total_bytes_received'].add_metric(
            [], self._total_bytes_received)

        self._prometheus_metrics['total_ops'].add_metric(
            [], self._total_ops)

        self._prometheus_metrics['total_successful_ops'].add_metric(
            [], self._total_successful_ops)

        for category in self._ops:
            self._prometheus_metrics['total_category_ops'].add_metric(
                [category], self._ops[category])

        for category in self._successful_ops:
            self._prometheus_metrics['total_category__successful_ops'].add_metric(
                [category], self._successful_ops[category])

        for metric in list(self._prometheus_metrics.values()):
            yield metric

    def _session(self):
        """
        Setup Requests connection settings.
        """
        self.session = requests.Session()
        self.session_adapter = requests.adapters.HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10)
        self.session.mount('http://', self.session_adapter)
        self.session.mount('https://', self.session_adapter)

        # Inversion of condition, when '--insecure' is defined we disable
        # requests warning about certificate hostname mismatch.
        if not self.insecure:
            warnings.filterwarnings('ignore', message='Unverified HTTPS request')
        if DEBUG:
            print("Perform insecured requests")

    def _request_data(self, query, args):
        """
        Requests data from RGW. If admin entry and caps is fine - return
        JSON data, otherwise return NoneType.
        """
        url = "{0}{1}/?format=json&{2}".format(self.url, query, args)

        try:
            response = self.session.get(url, verify=self.insecure,
                                        auth=S3Auth(self.access_key,
                                                    self.secret_key,
                                                    self.host))

            if response.status_code == requests.codes.ok:
                if DEBUG:
                    print(response)
                return response.json()
            else:
                # Usage caps absent or wrong admin entry
                print(("Request error [{0}]: {1}".format(
                    response.status_code, response.content.decode('utf-8'))))
                return

        # DNS, connection errors, etc
        except requests.exceptions.RequestException as e:
            print("Request error: {0}".format(e))
            return

    def _setup_empty_prometheus_metrics(self):
        """
        The metrics we want to export.
        """

        self._prometheus_metrics = {
            'ops':
                CounterMetricFamily('radosgw_usage_ops_total',
                                    'Number of operations',
                                    labels=["bucket", "owner", "category", "cluster", "tags"]),
            'successful_ops':
                CounterMetricFamily('radosgw_usage_successful_ops_total',
                                    'Number of successful operations',
                                    labels=["bucket", "owner", "category", "cluster", "tags"]),
            'bytes_sent':
                CounterMetricFamily('radosgw_usage_sent_bytes_total',
                                    'Bytes sent by the RADOSGW',
                                    labels=["bucket", "owner", "category", "cluster", "tags"]),
            'bytes_received':
                CounterMetricFamily('radosgw_usage_received_bytes_total',
                                    'Bytes received by the RADOSGW',
                                    labels=["bucket", "owner", "category", "cluster", "tags"]),
            'bucket_usage_bytes':
                GaugeMetricFamily('radosgw_usage_bucket_bytes',
                                  'Bucket used bytes',
                                  labels=["bucket", "owner", "zonegroup", "cluster", "tags"]),
            'bucket_bytes_sent':
                GaugeMetricFamily('radosgw_usage_bucket_bytes_sent',
                                  'Number of bytes sent',
                                  labels=["bucket", "owner", "cluster"]),
            'bucket_bytes_received':
                GaugeMetricFamily('radosgw_usage_bucket_bytes_received',
                                  'Number of bytes received',
                                  labels=["bucket", "owner", "cluster"]),
            'bucket_successful_ops':
                GaugeMetricFamily('radosgw_usage_bucket_successful_ops',
                                  'Number of successful operations',
                                  labels=["bucket", "owner", "cluster"]),
            'bucket_ops':
                GaugeMetricFamily('radosgw_usage_bucket_ops',
                                  'Number of operations',
                                  labels=["bucket", "owner", "cluster"]),
            'bucket_utilized_bytes':
                GaugeMetricFamily('radosgw_usage_bucket_utilized_bytes',
                                  'Bucket utilized bytes',
                                  labels=["bucket", "owner", "zonegroup", "cluster", "tags"]),
            'bucket_usage_objects':
                GaugeMetricFamily('radosgw_usage_bucket_objects',
                                  'Number of objects in bucket',
                                  labels=["bucket", "owner", "zonegroup", "cluster", "tags"]),
            'bucket_quota_enabled':
                GaugeMetricFamily('radosgw_usage_bucket_quota_enabled',
                                  'Quota enabled for bucket',
                                  labels=["bucket", "owner", "zonegroup", "cluster", "tags"]),
            'bucket_quota_max_size':
                GaugeMetricFamily('radosgw_usage_bucket_quota_size',
                                  'Maximum allowed bucket size',
                                  labels=["bucket", "owner", "zonegroup", "cluster", "tags"]),
            'bucket_quota_max_size_bytes':
                GaugeMetricFamily('radosgw_usage_bucket_quota_size_bytes',
                                  'Maximum allowed bucket size in bytes',
                                  labels=["bucket", "owner", "zonegroup", "cluster", "tags"]),
            'bucket_quota_max_objects':
                GaugeMetricFamily('radosgw_usage_bucket_quota_size_objects',
                                  'Maximum allowed bucket size in number of objects',
                                  labels=["bucket", "owner", "zonegroup", "cluster", "tags"]),
            'bucket_shards':
                GaugeMetricFamily('radosgw_usage_bucket_shards',
                                  'Number ob shards in bucket',
                                  labels=["bucket", "owner", "zonegroup", "cluster", "tags"]),
            'user_metadata':
                GaugeMetricFamily('radosgw_user_metadata',
                                  'User metadata',
                                  labels=["user", "display_name", "email", "storage_class", "cluster"]),
            'user_quota_enabled':
                GaugeMetricFamily('radosgw_usage_user_quota_enabled',
                                  'User quota enabled for bucket',
                                  labels=["user", "cluster"]),
            'user_quota_max_size':
                GaugeMetricFamily('radosgw_usage_user_quota_size',
                                  'Maximum allowed bucket size for user',
                                  labels=["user", "cluster"]),
            'user_quota_max_size_bytes':
                GaugeMetricFamily('radosgw_usage_user_quota_size_bytes',
                                  'Maximum allowed bucket size in bytes for user',
                                  labels=["user", "cluster"]),
            'user_quota_max_objects':
                GaugeMetricFamily('radosgw_usage_user_quota_size_objects',
                                  'Maximum allowed bucket size in number of objects',
                                  labels=["user", "cluster"]),
            'user_total_objects':
                GaugeMetricFamily('radosgw_usage_user_total_objects',
                                  'Usage of objects by user',
                                  labels=["user", "cluster"]),
            'user_total_bytes':
                GaugeMetricFamily('radosgw_usage_user_total_bytes',
                                  'Usage of bytes by user',
                                  labels=["user", "cluster"]),
            'user_total_bytes_sent':
                GaugeMetricFamily('radosgw_usage_user_total_bytes_sent',
                                  'Number of bytes sent',
                                  labels=["user", "cluster"]),

            'user_total_bytes_received':
                GaugeMetricFamily('radosgw_usage_user_total_bytes_received',
                                  'Number of bytes received',
                                  labels=["user", "cluster"]),

            'user_bytes_sent':
                GaugeMetricFamily('radosgw_usage_user_bytes_sent',
                                  'Number of bytes sent',
                                  labels=["user", "cluster", "category"]),

            'user_bytes_received':
                GaugeMetricFamily('radosgw_usage_user_bytes_received',
                                  'Number of bytes received',
                                  labels=["user", "cluster", "category"]),

            'user_total_ops':
                GaugeMetricFamily('radosgw_usage_user_total_ops',
                                  'Number of operations',
                                  labels=["user", "cluster"]),

            'user_ops':
                GaugeMetricFamily('radosgw_usage_user_ops',
                                  'Number of operations',
                                  labels=["user", "cluster", "category"]),

            'user_successful_ops':
                GaugeMetricFamily('radosgw_usage_user_successful_ops',
                                  'Number of operations',
                                  labels=["user", "cluster", "category"]),

            'user_total_successful_ops':
                GaugeMetricFamily('radosgw_usage_user_total_successful_ops',
                                  'Number of successful operations',
                                  labels=["user", "cluster"]),

            'user_total_buckets':
                GaugeMetricFamily('radosgw_usage_user_total_buckets',
                                  'Number of buckets per user',
                                  labels=["user", "cluster"]),

            'scrape_duration_seconds':
                GaugeMetricFamily('radosgw_usage_scrape_duration_seconds',
                                  'Ammount of time each scrape takes',
                                  labels=[]),

            'total_buckets':
                GaugeMetricFamily('radosgw_usage_total_buckets',
                                  'Number of buckets',
                                  labels=[]),

            'total_users':
                GaugeMetricFamily('radosgw_usage_total_users',
                                  'Number of users',
                                  labels=[]),

            'total_objects':
                GaugeMetricFamily('radosgw_usage_total_objects',
                                  'Usage of objects by user',
                                  labels=[]),
            'total_bytes':
                GaugeMetricFamily('radosgw_usage_total_bytes',
                                  'Usage of bytes by user',
                                  labels=[]),
            'total_bytes_sent':
                GaugeMetricFamily('radosgw_usage_total_bytes_sent',
                                  'Number of bytes sent',
                                  labels=[]),

            'total_bytes_received':
                GaugeMetricFamily('radosgw_usage_total_bytes_received',
                                  'Number of bytes received',
                                  labels=[]),

            'total_ops':
                GaugeMetricFamily('radosgw_usage_total_ops',
                                  'Number of operations',
                                  labels=[]),

            'total_successful_ops':
                GaugeMetricFamily('radosgw_usage_total_successful_ops',
                                  'Number of successful operations',
                                  labels=[]),

            'total_category_ops':
                GaugeMetricFamily('radosgw_usage_ops',
                                  'Number of operations',
                                  labels=["category"]),

            'total_category_successful_ops':
                GaugeMetricFamily('radosgw_usage_successful_ops',
                                  'Number of successful operations',
                                  labels=["category"]),
        }

    def _skip_bucket(self, bucket):
        if not SKIP_DELETED_BUCKET:
            return False

        creations = -1
        deletions = -1
        if 'categories' in bucket:
            for category in bucket['categories']:
                if 'category' in category:
                    if category['category'] == 'create_bucket':
                        creations = category['successful_ops']
                    elif category['category'] == 'delete_bucket':
                        deletions = category['successful_ops']

        # Skips bucket deleted or never created. This cover bucket that were already there before enabling the exporter
        if (creations == -1 and deletions > 0) or creations == 0 or (creations >= 0 and (deletions - creations) >= 0):
            if DEBUG:
                print('Bucket', bucket['bucket'], 'deleted or never created. skipping...')
            return True

        return False


    def _skip_user(self, user, rgw_users):
        if not SKIP_DELETED_USER:
            return False

        if 'owner' in user:
            u = user['owner']
        # Luminous
        elif 'user' in user:
            u = user['user']

        if u not in rgw_users:
            print('User', u, 'deleted or never created. skipping...')
            return True

        return False


    def _get_usage(self, entry):
        """
        Receives JSON object 'entity' that contains all the buckets relating
        to a given RGW UID. Builds a dictionary of metric data in order to
        handle UIDs where the usage data is truncated into multiple 1000
        entry bins.
        """

        if 'owner' in entry:
            bucket_owner = entry['owner']
        # Luminous
        elif 'user' in entry:
            bucket_owner = entry['user']

        if bucket_owner not in list(self.usage_dict.keys()):
            self.usage_dict[bucket_owner] = defaultdict(dict)

        for bucket in entry['buckets']:
            if DEBUG:
                print((json.dumps(bucket, indent=4, sort_keys=True)))

            if self._skip_bucket(bucket):
                continue

            if not bucket['bucket']:
                bucket_name = "bucket_root"
            else:
                bucket_name = bucket['bucket']

            if bucket_name not in list(self.usage_dict[bucket_owner].keys()):
                self.usage_dict[bucket_owner][bucket_name] = defaultdict(dict)

            for category in bucket['categories']:
                category_name = category['category']
                if category_name not in list(self.usage_dict[bucket_owner][bucket_name].keys()):
                    self.usage_dict[bucket_owner][bucket_name][category_name] = Counter()
                c = self.usage_dict[bucket_owner][bucket_name][category_name]
                c.update({'ops': category['ops'],
                          'successful_ops': category['successful_ops'],
                          'bytes_sent': category['bytes_sent'],
                          'bytes_received': category['bytes_received']})

    def _update_usage_metrics(self):
        """
        Update prometheus metrics with bucket usage data
        """

        for bucket_owner in list(self.usage_dict.keys()):
            for bucket_name in list(self.usage_dict[bucket_owner].keys()):
                bucket = self.usage_dict[bucket_owner][bucket_name]
                if self._skip_bucket(bucket):
                    continue

                for category in list(bucket.keys()):
                    data_dict = self.usage_dict[bucket_owner][bucket_name][category]
                    self._prometheus_metrics['ops'].add_metric(
                        [bucket_name, bucket_owner, category, self.cluster_name],
                        data_dict['ops'])

                    self._prometheus_metrics['successful_ops'].add_metric(
                        [bucket_name, bucket_owner, category, self.cluster_name],
                        data_dict['successful_ops'])

                    self._prometheus_metrics['bytes_sent'].add_metric(
                        [bucket_name, bucket_owner, category, self.cluster_name],
                        data_dict['bytes_sent'])

                    self._prometheus_metrics['bytes_received'].add_metric(
                        [bucket_name, bucket_owner, category, self.cluster_name],
                        data_dict['bytes_received'])

    def _update_usage_summary_metrics(self, summary):
        """
        Update prometheus metrics with user total usage data
        """

        if 'owner' in summary:
            user = summary['owner']
        # Luminous
        elif 'user' in summary:
            user = summary['user']

        if DEBUG:
            print('User summary', (json.dumps(summary, indent=4, sort_keys=True)))
        if 'categories' in summary:
            for category in summary['categories']:
                self._prometheus_metrics['user_bytes_sent'].add_metric(
                    [user, self.cluster_name, category['category']], category['bytes_sent'])
                self._prometheus_metrics['user_bytes_received'].add_metric(
                    [user, self.cluster_name, category['category']], category['bytes_received'])
                self._prometheus_metrics['user_ops'].add_metric(
                    [user, self.cluster_name, category['category']], category['ops'])
                if category['category'] not in self._ops:
                    self._ops[category['category']] = 0
                self._ops[category['category']] += category['ops']

                self._prometheus_metrics['user_successful_ops'].add_metric(
                    [user, self.cluster_name, category['category']], category['successful_ops'])
                if category['category'] not in self._ops:
                    self._ops[category['category']] = 0
                self._ops[category['category']] += category['successful_ops']

        if 'total' in summary:
            self._prometheus_metrics['user_total_bytes_sent'].add_metric(
                [user, self.cluster_name], summary['total']['bytes_sent'])
            self._total_bytes_sent += summary['total']['bytes_sent']

            self._prometheus_metrics['user_total_bytes_received'].add_metric(
                [user, self.cluster_name], summary['total']['bytes_received'])
            self._total_bytes_received += summary['total']['bytes_received']

            self._prometheus_metrics['user_total_ops'].add_metric(
                [user, self.cluster_name], summary['total']['ops'])
            self._total_ops += summary['total']['ops']

            self._prometheus_metrics['user_total_successful_ops'].add_metric(
                [user, self.cluster_name], summary['total']['successful_ops'])
            self._total_successful_ops += summary['total']['successful_ops']

        if user in self.user_buckets:
            self._prometheus_metrics['user_total_buckets'].add_metric(
                [user, self.cluster_name], self.user_buckets[user])

    def _get_bucket_usage(self, bucket):
        """
        Method get actual bucket usage (in bytes).
        Some skips and adjustments for various Ceph releases.
        """

        if self._skip_bucket(bucket):
            return

        if DEBUG:
            print((json.dumps(bucket, indent=4, sort_keys=True)))

        if type(bucket) is dict:
            bucket_name = bucket['bucket']
            bucket_owner = bucket['owner']
            bucket_shards = bucket['num_shards']
            bucket_usage_bytes = 0
            bucket_utilized_bytes = 0
            bucket_usage_objects = 0
            if bucket_owner not in self.user_buckets:
                self.user_buckets[bucket_owner] = 0
            self.user_buckets[bucket_owner] = self.user_buckets[bucket_owner] + 1

            if bucket['usage'] and 'rgw.main' in bucket['usage']:
                # Prefer bytes, instead kbytes
                if 'size_actual' in bucket['usage']['rgw.main']:
                    bucket_usage_bytes = bucket['usage']['rgw.main']['size_actual']
                # Hammer don't have bytes field
                elif 'size_kb_actual' in bucket['usage']['rgw.main']:
                    usage_kb = bucket['usage']['rgw.main']['size_kb_actual']
                    bucket_usage_bytes = usage_kb * 1024

                # Compressed buckets, since Kraken
                if 'size_utilized' in bucket['usage']['rgw.main']:
                    bucket_utilized_bytes = bucket['usage']['rgw.main']['size_utilized']

                # Get number of objects in bucket
                if 'num_objects' in bucket['usage']['rgw.main']:
                    bucket_usage_objects = bucket['usage']['rgw.main']['num_objects']

            if 'zonegroup' in bucket:
                bucket_zonegroup = bucket['zonegroup']
            # Hammer
            else:
                bucket_zonegroup = "0"

            if 'tagset' in bucket:
                bucket_tagset = bucket['tagset']
                taglist = ", ".join("=".join((k, str(v)))
                                    for k, v in sorted(bucket_tagset.items()))
            else:
                taglist = ''

            self._prometheus_metrics['bucket_usage_bytes'].add_metric(
                [bucket_name, bucket_owner, bucket_zonegroup, self.cluster_name, taglist],
                bucket_usage_bytes)

            self._prometheus_metrics['bucket_utilized_bytes'].add_metric(
                [bucket_name, bucket_owner, bucket_zonegroup, self.cluster_name, taglist],
                bucket_utilized_bytes)

            self._prometheus_metrics['bucket_usage_objects'].add_metric(
                [bucket_name, bucket_owner, bucket_zonegroup, self.cluster_name, taglist],
                bucket_usage_objects)

            if 'bucket_quota' in bucket:
                self._prometheus_metrics['bucket_quota_enabled'].add_metric(
                    [bucket_name, bucket_owner, bucket_zonegroup, self.cluster_name, taglist],
                    bucket['bucket_quota']['enabled'])
                self._prometheus_metrics['bucket_quota_max_size'].add_metric(
                    [bucket_name, bucket_owner, bucket_zonegroup, self.cluster_name, taglist],
                    bucket['bucket_quota']['max_size'])
                self._prometheus_metrics['bucket_quota_max_size_bytes'].add_metric(
                    [bucket_name, bucket_owner, bucket_zonegroup, self.cluster_name, taglist],
                    bucket['bucket_quota']['max_size_kb'] * 1024)
                self._prometheus_metrics['bucket_quota_max_objects'].add_metric(
                    [bucket_name, bucket_owner, bucket_zonegroup, self.cluster_name, taglist],
                    bucket['bucket_quota']['max_objects'])

            self._prometheus_metrics['bucket_shards'].add_metric(
                [bucket_name, bucket_owner, bucket_zonegroup, self.cluster_name, taglist],
                bucket_shards)

        else:
            # Hammer junk, just skip it
            pass

    def _update_bucket_usage_summary_metrics(self, user_entry, rgw_users):
        if 'buckets' in user_entry:
            for bucket in user_entry['buckets']:
                bucket_name = bucket['bucket']
                bucket_owner = bucket['owner']
                if self._skip_bucket(bucket) or self._skip_user(bucket, rgw_users):
                    continue

                bytes_sent = 0
                successful_ops = 0
                bytes_received = 0
                ops = 0
                if 'categories' in bucket:
                    for category in bucket['categories']:
                        bytes_sent = bytes_sent + category['bytes_sent']
                        bytes_received = bytes_received + category['bytes_received']
                        ops = ops + category['ops']
                        successful_ops = successful_ops + category['successful_ops']

                if DEBUG:
                    print('Bucket usage summary', (json.dumps(bucket, indent=4, sort_keys=True)))

                self._prometheus_metrics['bucket_ops'].add_metric(
                    [bucket_name, bucket_owner, self.cluster_name], ops)
                self._prometheus_metrics['bucket_successful_ops'].add_metric(
                    [bucket_name, bucket_owner, self.cluster_name], successful_ops)
                self._prometheus_metrics['bucket_bytes_sent'].add_metric(
                    [bucket_name, bucket_owner, self.cluster_name], bytes_sent)
                self._prometheus_metrics['bucket_bytes_received'].add_metric(
                    [bucket_name, bucket_owner, self.cluster_name], bytes_received)

    def _get_rgw_users(self):
        """
        API request to get users.
        """

        rgw_users = self._request_data(query='user', args='list')

        if rgw_users and 'keys' in rgw_users:
            return rgw_users['keys']
        else:
            # Compat with old Ceph versions (pre 12.2.13/13.2.9)
            rgw_metadata_users = self._request_data(query='metadata/user', args='')
            return rgw_metadata_users

        return

    def _get_user_quota(self, user):
        """
        Method to get the quota set on a specific user(s).
        """
        quota = self._request_data(query='user', args="quota&uid={0}&quota-type=user".format(user))

        if DEBUG:
            print('User quota\n', (json.dumps(quota, indent=4, sort_keys=True)))

        self._prometheus_metrics['user_quota_enabled'].add_metric(
            [user, self.cluster_name], quota['enabled'])
        self._prometheus_metrics['user_quota_max_size'].add_metric(
            [user, self.cluster_name], quota['max_size'])
        self._prometheus_metrics['user_quota_max_size_bytes'].add_metric(
            [user, self.cluster_name], quota['max_size_kb'] * 1024)
        self._prometheus_metrics['user_quota_max_objects'].add_metric(
            [user, self.cluster_name], quota['max_objects'])

    def _get_user_info(self, user):
        """
        Method to get the info on a specific user(s).
        """
        user_info = self._request_data(query='user', args="uid={0}&stats=True".format(user))

        if DEBUG:
            print('User info\n', (json.dumps(user_info, indent=4, sort_keys=True)))

        if 'display_name' in user_info:
            user_display_name = user_info['display_name']
        else:
            user_display_name = ""
        if 'email' in user_info:
            user_email = user_info['email']
        else:
            user_email = ""
        # Nautilus+
        if 'default_storage_class' in user_info:
            user_storage_class = user_info['default_storage_class']
        else:
            user_storage_class = ""

        self._prometheus_metrics['user_metadata'].add_metric(
            [user, user_display_name, user_email, user_storage_class, self.cluster_name], 1)

        if 'stats' in user_info:
            self._prometheus_metrics['user_total_bytes'].add_metric(
                [user, self.cluster_name], user_info['stats']['size_actual'])
            self._total_bytes += user_info['stats']['size_actual']

            self._prometheus_metrics['user_total_objects'].add_metric(
                [user, self.cluster_name], user_info['stats']['num_objects'])
            self._total_objects += user_info['stats']['num_objects']


def parse_args():
    parser = argparse.ArgumentParser(
        description='RADOSGW address and local binding port as well as \
        S3 access_key and secret_key'
    )
    parser.add_argument(
        '-H', '--host',
        required=False,
        help='Server URL for the RADOSGW api (example: http://objects.dreamhost.com/)',
        default=os.environ.get('RADOSGW_SERVER', 'http://radosgw:80')
    )
    parser.add_argument(
        '-e', '--admin_entry',
        required=False,
        help="The entry point for an admin request URL [default is '%(default)s']",
        default=os.environ.get('ADMIN_ENTRY', 'admin')
    )
    parser.add_argument(
        '-a', '--access_key',
        required=False,
        help='S3 access key',
        default=os.environ.get('ACCESS_KEY', 'NA')
    )
    parser.add_argument(
        '-s', '--secret_key',
        required=False,
        help='S3 secrest key',
        default=os.environ.get('SECRET_KEY', 'NA')
    )
    parser.add_argument(
        '-k', '--insecure',
        help='Allow insecure server connections when using SSL',
        action="store_false"
    )
    parser.add_argument(
        '-p', '--port',
        required=False,
        type=int,
        help='Port to listen',
        default=int(os.environ.get('VIRTUAL_PORT', '9242'))
    )
    parser.add_argument(
        '-c', '--cluster',
        required=False,
        help='cluster name',
        default=os.environ.get('CLUSTER_NAME', 'ceph'),
    )

    return parser.parse_args()


def main():
    try:
        args = parse_args()
        REGISTRY.register(RADOSGWCollector(
            args.host, args.admin_entry, args.access_key, args.secret_key, args.cluster, args.insecure))
        start_http_server(args.port)
        print("Polling {0}. Serving at port: {1}".format(args.host, args.port))
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        print("\nInterrupted")
        exit(0)


if __name__ == "__main__":
    main()
