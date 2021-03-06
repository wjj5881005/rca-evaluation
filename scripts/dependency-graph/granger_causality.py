# system modules
import os
import sys
import json
import math
import itertools
import pandas as pd
import numpy as np
import argparse
import metrics_utils as msu

from collections import defaultdict
from statsmodels.tsa.stattools import grangercausalitytests, adfuller

DEFAULT_CALLGRAPH_FILE_PATH = "openstack-callgraph.dot"

# custom modules
import metadata
from kshape import zscore, _sbd
from preprocess import interpolate_missing

APP_METRIC_DELIMITER = "|"

def preferred_cluster(clusters):
    preferred = 0
    preferred_value = -1
    for k, v in clusters.items():
        if v["silhouette_score"] > preferred_value:
            preferred = int(k)
            preferred_value = v["silhouette_score"]
    return preferred

def best_column_of_cluster(service, filenames, path, prev_cluster_metadata=None):
    selected_columns = {}
    index = None
    # representative metrics, per cluster index
    rep_metrics = dict()

    for i, filename in enumerate(filenames):

        best_distance = np.inf
        best_column = None

        cluster_path = os.path.join(path, filename)
        df = pd.read_csv(cluster_path, sep="\t", index_col='time', parse_dates=True)
        # pick the rep, metric as the one w/ shortest distance to the 'centroid'
        for c in df.columns:
            if c == "centroid":
                continue
            distance = _sbd(df.centroid, df[c])[0]
            if distance < best_distance:
                best_distance = distance
                best_column = c

        # if we're deriving clusters from prev. version assigments, a rep. metric 
        # switch may happen
        if prev_cluster_metadata != None:
            # get cluster assigments from prev. version
            s_score, cluster_metrics = msu.get_cluster_metrics(prev_cluster_metadata, service)

            for key, values in cluster_metrics.iteritems():
                # if one finds a cluster w/ the same rep. metrics, break
                if values['rep_metric'] == best_column:
                    break
                # else, look for cases in which some prev. rep. metric is 
                # part of the current cluster and the current rep. metric is 
                # part of the cluster represented by that prev. metric.
                elif values['rep_metric'] in df.columns and best_column in values['other_metrics']:
                    # in this case, switch the rep metrics, since the clusters 
                    # should be 'similar'
                    best_column = values['rep_metric']

        if best_column != None:
            selected_columns[best_column] = df[best_column]
            rep_metrics[i] = str(best_column)

    return rep_metrics, pd.DataFrame(data=selected_columns, index=df.index)

def read_service(srv, path, prev_cluster_metadata):
    preferred = preferred_cluster(srv["clusters"])
    if preferred == 0:
        srv_path = os.path.join(path, srv["preprocessed_filename"])
        df = pd.read_csv(srv_path, sep="\t", index_col='time', parse_dates=True)
        for c in df.columns:
            df[c] = zscore(df[c])
    else:
        cluster = srv["clusters"][str(preferred)]
        rep_metrics, df = best_column_of_cluster(srv["name"], cluster["filenames"], path, prev_cluster_metadata)

    # write additional metadata about components:
    #   - the preferred cluster for the component (is it really necessary?)
    #   - the representative metrics for each cluster
    with metadata.update(path) as data:
        for _service in data["services"]:
            if _service["name"] == srv["name"]:
                if "pref_cluster" not in _service:
                    _service["pref_cluster"] = preferred
                if preferred == 0:
                    continue
                if "rep_metrics" not in _service["clusters"][str(preferred)]:
                    _service["clusters"][str(preferred)]["rep_metrics"] = rep_metrics

    new_names = []
    for column in df.columns:
        if column.startswith(srv["name"]):
            new_names.append(column)
        else:
            new_names.append(srv["name"] + APP_METRIC_DELIMITER + column)
    df.columns = new_names
    return df

def grangercausality(df, p_values, lags):
    c1 = df.columns[0]
    c2 = df.columns[1]
    try:
        res = grangercausalitytests(df, lags, verbose=False)
    except Exception as e:
#        if df[c1].var() < 1e-30:
#            print("low variance for %s, got: %s" % (c1, e))
#        if df[c2].var() < 1e-30:
#            print("low variance for %s, got: %s" % (c2, e))
#        else:
#            print("error while processing %s -> %s, got: %s" % (c1, c2, e))
        return
    p_values["perpetrator"].append(c1)
    p_values["consequence"].append(c2)
    for i in range(lags):
        lag = i + 1
        p_values["p_for_lag_%d" % lag].append(res[lag][0]['ssr_ftest'][1])

def combine(a, b):
    for x in a:
        for y in b:
            yield(x,y)

def _compare_services(srv_a, srv_b, path, prev_cluster_metadata):
    df_a = read_service(srv_a, path, prev_cluster_metadata)
    df_b = read_service(srv_b, path, prev_cluster_metadata)
    p_values = defaultdict(list)
    df = pd.concat([df_a, df_b]).resample("500ms").mean()
    df.interpolate(method="time", limit_direction="both", inplace=True)
    df.fillna(method="bfill", inplace=True)

    for c1, c2 in combine(df_a.columns, df_b.columns):
        if c1 == c2:
            continue
        grangercausality(df[[c1, c2]], p_values, 5)
        grangercausality(df[[c2, c1]], p_values, 5)
    return pd.DataFrame(p_values)

def compare_services(srv_a, srv_b, path, prev_cluster_metadata):
    print("%s -> %s" % (srv_a["name"], srv_b["name"]))
    causality_file = os.path.join(path, "%s-%s-causality.tsv.gz" % (srv_a["name"], srv_b["name"]))
    if os.path.exists(causality_file):
        print("skip %s" % causality_file)
        return
    df = _compare_services(srv_a, srv_b, path, prev_cluster_metadata)
    df.to_csv(causality_file, sep="\t", compression="gzip")

SERVICES = ["horizon", 
    "heat_engine", 
    "heat_api_cfn", 
    "heat_api:heat_api", 
    "neutron_metadata_agent", 
    "neutron_l3_agent", 
    "neutron_dhcp_agent", 
#    "neutron_openvswitch_agent", 
    "neutron_server:neutron_api_local_check", 
    "openvswitch_vswitchd", 
    "nova_ssh", 
    "nova_compute:nova_cloud_stats", 
    "nova_libvirt", 
    "nova_conductor", 
    "nova_scheduler", 
    "nova_novncproxy", 
    "nova_consoleauth", 
    "nova_api:nova_api_local_check:nova_api_metadata_local_check", 
    "glance_api:glance_api_local_check", 
    "glance_registry:glance_registry_local_check", 
    "keystone", 
    "rabbitmq:rabbitmq_overview:rabbitmq_node:rabbitmq_queue", 
    "mariadb", 
    "memcached:memcached", 
    "keepalived", 
    "haproxy", 
    "cron", 
    "kolla_toolbox", 
    "heka",
#    "swift_proxy_server",
    "swift_object_updater",
    "swift_object_replicator",
    "swift_object_auditor",
    "swift_object_server",
    "swift_container_updater",
    "swift_container_replicator",
    "swift_container_auditor",
    "swift_container_server",
    "swift_account_reaper",
    "swift_account_replicator",
    "swift_account_auditor",
    "swift_account_server",
    "swift_rsyncd"]

# services with which every other service communicates but may not be present 
# in callgraph .dot file (e.g. AMQP connections to rabbitmq)
COMMON_SERVICES = ["rabbitmq"]
# the main openstack service prefixes
MAIN_SERVICES = ["nova", "neutron", "glance", "swift", "cinder"]

def extract_callgraph_pairs(callgraph_file_path):

    # the final result will be a collection of unique related pairs
    callgraph_pairs = {}

    print [s.split(":", 1)[0] for s in SERVICES]

    with open(callgraph_file_path, "r") as f:
        for line in f.readlines():
            # consider the edge lines of the .dot file only
            if not "->" in line:
                continue
            # split the line in (hopefully 2) parts: perpetrating service and 
            # consequence. discard the line if more than 2 parts are generated.
            line = line.split(" -> ", 1)
            if len(line) < 2:
                continue

            # extract each of the services
            a = (line[0].lstrip('\"')).rstrip('\"\n')
            b = (line[1].lstrip('\"')).rstrip('\"\n')
            # consider relationships between different services only, and only 
            # relationships for components listed in SERVICES
            print("%s > %s" % (a, b))
            if a != b and set([a, b]).issubset(set([s.split(":", 1)[0] for s in SERVICES])):
                x = a
                y = b
                if x > y:
                    x, y = y, x
                callgraph_pairs[x + y] = (x, y)

    # also, include relationships between services which share a prefix, 
    # e.g. "neutron_l3_agent" and "neutron_server"
    for a in SERVICES:

        # separate the service name from the related metrics
        a = a.split(":", 1)[0]
        # isolate the main service name (before the '_', e.g. 'neutron' in 
        # 'neutron_server'
        a_prefix = a.split("_")[0]

        if a_prefix not in MAIN_SERVICES:
            continue

        for b in COMMON_SERVICES:
            if a != b:
                x = a
                y = b
                if x > y:
                    x, y = y, x
                callgraph_pairs[x + y] = (x, y)

    return callgraph_pairs

def find_causality(metadata_path, callgraph_file_path, prev_cluster_metadata):

    # extract the service pairs from the callgraph (.dot file)
    callgraph_pairs = extract_callgraph_pairs(callgraph_file_path)

    # load the metadata.json which summarizes the measurement dir info. extract 
    # the names of the services.
    data = metadata.load(metadata_path)
    services = {}
    for srv in data["services"]:
        services[srv["name"]] = srv

    # determine granger causality between services
    for srv_a, srv_b in callgraph_pairs.values():
        compare_services(services[srv_a], services[srv_b], metadata_path, prev_cluster_metadata)

if __name__ == '__main__':

    # use an ArgumentParser for a nice CLI
    parser = argparse.ArgumentParser()

    # options (self-explanatory)
    parser.add_argument(
        "--msr-dir", 
         help = """dir w/ clustered data.""")

    parser.add_argument(
        "--initial-cluster-dir", 
         help = """dir w/ clustered data from which to get prev. cluster 
                   assigments.""")

    parser.add_argument(
        "--callgraph", 
         help = """path to callgraph .dot file. default is 'openstack-callgraph.dot' 
                (on the local dir).""")

    args = parser.parse_args()

    # quit if a dir w/ measurement files hasn't been provided
    if not args.msr_dir:
        sys.stderr.write("""%s: [ERROR] please pass a dir w/ clustered data as '--msr-dir'\n""" % sys.argv[0]) 
        parser.print_help()
        sys.exit(1)

    if args.initial_cluster_dir:
        prev_cluster_metadata = metadata.load(args.initial_cluster_dir)
    else:
        prev_cluster_metadata = None

    # choose the default .dot callgraph if one hasn't been provided
    if not args.callgraph:
        callgraph_file_path = DEFAULT_CALLGRAPH_FILE_PATH
    else:
        callgraph_file_path = args.callgraph

    find_causality(args.msr_dir, callgraph_file_path, prev_cluster_metadata)

