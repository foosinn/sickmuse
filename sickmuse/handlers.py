import glob
import os
import copy
import json
import re
import rrdtool

from tornado.web import RequestHandler, HTTPError
from collections import defaultdict
from datetime import datetime

from sickmuse import __version__


DATE_RANGE_INFO = (
    ('24hr', {'label': 'Past 24 hours', 'start': '-1d', 'resolution': 300}),
    ('1hr', {'label': 'Past hour', 'start': '-1h', 'resolution': 60}),
    ('3hr', {'label': 'Past 3 hours', 'start': '-3h', 'resolution': 3600}),
    ('6hr', {'label': 'Past 6 hours', 'start': '-6h', 'resolution': 3600}),
    ('12hr', {'label': 'Past 12 hours', 'start': '-12h', 'resolution': 3600}),
    ('1week', {'label': 'Past week', 'start': '-1w', 'resolution': 604800}),
    ('1mon', {'label': 'Past month', 'start': '-1mon', 'resolution': 2678400}),
    ('3mon', {'label': 'Past 3 months', 'start': '-3mon', 'resolution': 2678400}),
    ('6mon', {'label': 'Past 6 months', 'start': '-6mon', 'resolution': 2678400}),
    ('1year', {'label': 'Past year', 'start': '-1y', 'resolution': 31622400}),
)

DATE_RANGE_MAP = dict(DATE_RANGE_INFO)


UNIT_MAP = {
    # Metric name --> Unit
    'memory': 'bytes',
    'partition': 'bytes',
    'pg_db_size': 'bytes',
    'ps_rss': 'bytes',
    'swap': 'bytes',
    'vs_memory': 'bytes',
}


class TemplateHandler(RequestHandler):
    "Add common elements to the template namespace."

    def get_template_namespace(self):
        namespace = super(TemplateHandler, self).get_template_namespace()
        namespace.update({
            'plugin_info': self.application.plugin_info,
            'debug': self.application.settings.get('debug', False),
            'static_url_prefix': self.application.settings.get('static_url_prefix', '/static/'),
            '__version__': __version__,
        })
        return namespace


class RootHandler(TemplateHandler):
    
    def get(self):
        self.render("index.html")


class HostHandler(TemplateHandler):
    
    def get(self, host_name):
        if host_name not in self.application.plugin_info:
            raise HTTPError(404, 'Host not found')
        context = {
            'host_name': host_name,
            'host_info': self.application.plugin_info[host_name],
            'date_range': DATE_RANGE_INFO,
        }
        self.render("host-detail.html", **context)


class APIList(RequestHandler):
    
    def get(self, host):
        if host not in self.application.plugin_info:
            self.write('404')
            self.finish()
        plugins = self.application.plugin_info[host].get('plugins', {})
        self.write(json.dumps(plugins.keys()))


class APIFast(RequestHandler):

    def get_host_plugin_data(self, hosts, metric):
        for host in hosts:
            if host not in self.application.plugin_info: continue
            plugins = self.application.plugin_info[host].get('plugins', {})
            if metric in plugins:
                yield host, metric, copy.deepcopy(plugins[metric])
            elif metric == 'cpu':
                path = os.path.join(self.application.settings['rrd_directory'], host)
                for w_path, _, _ in os.walk(path):
                    if re.search('cpu-[0-9]+$', w_path):
                        plugin_name = w_path.split('/')[-1]
                        yield host, plugin_name, copy.deepcopy(plugins[plugin_name])

    def timestring(self, time, offset):
        time = datetime.fromtimestamp(time)
        if offset in ['1hr', '3hr', '6hr', '12hr']:
            return time.strftime('%X')
        elif offset in ['24hr', '1week', '1mon']:
            return time.strftime('%m-%d %H:%M')
        else:
            return time.strftime('%y-%m-%d')

    def get(self, hosts, metric):
        hosts = hosts.split(',')
        
        # removed multihost support
        if len(hosts) != 1:
            self.write('404')
            self.finish()

        offset = self.get_argument('range', default='24hr')
        date_range = DATE_RANGE_MAP[offset]
        request_start = str(date_range['start'])
        request_res = str(date_range['resolution'])

        headers = ['Time']
        header_to_index = {}
        time_data = []

        for host, metric, plugin_graphs in self.get_host_plugin_data(hosts, metric):

            # remove unwanted items
            #if metric.startswith('cpu-'):
            #    plugin_graphs.remove('cpu-idle')
            if metric == 'df-local':
                plugin_graphs.remove('df_complex-reserved')

            for plugin_graph in plugin_graphs:
                f_path = str(os.path.join(self.application.settings['rrd_directory'], host, metric, '%s.rrd' % plugin_graph))
                period, metrics, data = rrdtool.fetch(f_path, 'AVERAGE', '--start', request_start, '--resolution', request_res)
                start, end, resolution = period

                # remove load graphs
                if metric == 'load':
                    metrics = (metrics[0], )
                    data = ((i[0], ) for i in data)
                
                # create headers and insert positons
                if len(metrics) == 1:
                    if plugin_graph not in headers: 
                        header_to_index[plugin_graph] = len(headers)
                        headers.append(plugin_graph)
                else:
                    for name in metrics:
                        header = '%s-%s' % (plugin_graph, name)
                        if header not in headers: 
                            header_to_index[plugin_graph] = len(plugin_graph)
                            headers.append(header)

                # put data into required format
                for time_data_pos, item in enumerate(data):
                    for item_pos, name in enumerate(metrics):
                        if len(metrics) == 1:
                            key = plugin_graph
                        else:
                            key = '%s-%s' % (plugin_graph, name)
                        if len(time_data) == time_data_pos:
                            time_data.insert(time_data_pos, [self.timestring(start + time_data_pos * resolution, offset)])
                        while len(time_data[time_data_pos]) <= header_to_index[key]:
                            time_data[time_data_pos].append(0)
                        value = item[item_pos] or 0
                        if metric in ['swap', 'df-local', 'memory']:
                            value = value / 1024**3
                        time_data[time_data_pos][header_to_index[key]] += value
        
        # round
        for value in (i for i in time_data):
            for i in range(1, len(value)):
                value[i] = round(value[i], 3)
                        
        self.write(json.dumps([headers] + time_data))


class MetricAPIHandler(RequestHandler):
    
    def get(self, host, metric):
        if host not in self.application.plugin_info:
            self.write('404')
            self.finish()
        plugins = self.application.plugin_info[host].get('plugins', {})
        if metric not in plugins:
            self.write('404')
            self.finish()
        instances = plugins[metric]
        cleaned_data = {
            'units': UNIT_MAP.get(metric),
        }
        instance_data = {}
        offset = self.get_argument('range', default='24hr')
        if offset not in DATE_RANGE_MAP:
            raise HTTPError(400, 'Invalid date range')
        date_range = DATE_RANGE_MAP[offset]
        
        if metric.startswith('cpu-'):
            instances.remove('cpu-idle')
        if metric == 'df-local':
            instances.remove('df_complex-reserved')

        for instance in instances:
            load_file = str(os.path.join(
                self.application.settings['rrd_directory'],
                host, metric, '%s.rrd' % instance
            ))
            start = str(date_range['start'])
            res = str(date_range['resolution'])
            period, metrics, data = rrdtool.fetch(load_file, 'AVERAGE', '--start', start, '--resolution', res)
            start, end, resolution = period
            default = {'start': start, 'end': end, 'resolution': resolution, 'timeline': []}

            if metric == 'load':
                metrics = (metrics[0], )
                data = ((i[0], ) for i in data)

            if len(metrics) == 1:
                key = instance
                instance_data[key] = default
            else:
                for name in metrics:
                    key = '%s-%s' % (instance, name)
                    instance_data[key] = default
            for item in data:
                for i, name in enumerate(metrics):
                    if len(metrics) == 1:
                        key = instance
                    else:
                        key = '%s-%s' % (instance, name)
                    instance_data[key]['timeline'].append(item[i])
        cleaned_data['instances'] = instance_data
        self.write(cleaned_data)
