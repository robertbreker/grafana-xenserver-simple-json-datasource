#!/usr/bin/env python

import argparse
import BaseHTTPServer
import dateutil.parser
import httplib2
import json
import time
import tokenize
import token
import StringIO


LOCAL_HOST = ''
PORT_NUMBER = 8080


def fixUnquotedParameters(json):
    tokens = tokenize.generate_tokens(StringIO.StringIO(json).readline)
    result = list()
    for toknum, tokval, _, _, _ in tokens:
        if toknum == token.NAME:
            tokval = '"%s"' % tokval
        result.append((toknum, tokval))
    return tokenize.untokenize(result)


class Grafana:

    _args = None

    def __init__(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('--xenserver-host', required=True)
        parser.add_argument('--xenserver-username', required=True)
        parser.add_argument('--xenserver-password', required=True)
        self._args = parser.parse_args()

    def _get_data(self, parameters, source):
        http = httplib2.Http()
        http.add_credentials(self._args.xenserver_username,
                             self._args.xenserver_password)
        parameter_string = ""
        for key, value in parameters.iteritems():
            if parameter_string:
                parameter_string += "&"
            parameter_string += "%s=%s" % (key, value)
        url = ("http://%s/%s?%s"
               % (self._args.xenserver_host, source, parameter_string))
        # print url
        resp, content = http.request(url, "GET")
        return {'response': resp,
                'content': content}

    def annotations(self, data):
        return json.dumps([])

    def query(self, data):
        vm_targets = []
        host_targets = []
        for target_info in data['targets']:
            if 'target' in target_info:
                if target_info['target'].startswith('AVERAGE:host:'):
                    host_targets.append(target_info['target'])
                else:
                    vm_targets.append(target_info['target'])
        response = []
        if len(host_targets) > 0:
            response.extend(self.do_query(data, host_targets, True))
        if len(vm_targets) > 0:
            response.extend(self.do_query(data, vm_targets, False))
        return_text = json.dumps(response)
        return return_text

    def do_query(self, data, targets_to_query, host_data):
        parameters = {}
        time_from = int(dateutil.parser.parse(
            data['range']['from']).strftime('%s'))
        time_to = int(dateutil.parser.parse(
            data['range']['to']).strftime('%s'))
        time_total = time_to - time_from
        time_overlap = int(time_total * 0.5)
        # add some overlap so the data starts before the graph
        parameters["start"] = time_from - time_overlap
        parameters["json"] = "true"
        if host_data:
            parameters["host"] = "true"
        targets = {}
        for target in targets_to_query:
            targets[target] = None
        json_data = self._get_data(parameters, 'rrd_updates')
        json_data = json.loads(fixUnquotedParameters(json_data['content']))
        i = 0
        for legend in json_data['meta']['legend']:
            if legend in targets:
                targets[legend] = i
            i = i + 1
        for target_key, target_position in targets.iteritems():
            if not target_position:
                raise Exception("Could not find RRD %s" % (target_key))
        response = []
        json_data['data'] = sorted(json_data['data'], key=lambda k: k['t'])
        for target_key, target_position in targets.iteritems():
            datapoints = []
            before_start = None
            for datapoint in json_data['data']:
                if datapoint['values'][target_position] != 'NaN':
                    if int(datapoint['t']) < int(time_from):
                        before_start = datapoint
                    elif int(datapoint['t']) <= int(time_to):
                        if before_start:
                            # we Print the first point before the asked
                            # timerange -to not start int he middle of a graph
                            datapoints.append([before_start['values'][
                                target_position],
                                before_start['t'] * 1000])
                            before_start = None
                        datapoints.append([datapoint['values'][
                            target_position],
                            datapoint['t'] * 1000,
                        ])
                    elif int(datapoint['t']) > int(time_to):
                        # we print one point after the requested range
                        # so the graph doesn't end in the middle
                        datapoints.append([datapoint['values'][
                            target_position],
                            datapoint['t'] * 1000,
                        ])
                        break

            response.append({'target': target_key,
                             'datapoints': datapoints})
        return response

    cached_search = None
    cached_search_time = None

    def search(self, data):
        if ((self.cached_search_time and
             time.time() - 10 < self.cached_search_time)):
            return self.cached_search
        parameters = {}
        parameters["cf"] = "AVERAGE"
        parameters["start"] = int(time.time()) - 1000
        parameters["json"] = "true"
        names = list()
        for query in [{}, {'host': 'true'}]:
            parameters.update(query)
            json_data = self._get_data(parameters, 'rrd_updates')
            json_data = json.loads(fixUnquotedParameters(json_data['content']))
            for legend in json_data['meta']['legend']:
                (accumulator, objecttype, uuid, name) = legend.split(':')
                # Let the client filter by itself, as the data['target'] is
                # purely informative
                names.append(legend)
        sorted(names, key=lambda s: s.lower())
        self.cached_search = json.dumps(names)
        self.cached_search_time = time.time()
        return self.cached_search


class MyHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    _grafana = Grafana()

    def do_header(self):
        self.send_response(200)
        self.send_header("Content-type", "text/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.send_header("Access-Control-Allow-Headers",
                         "accept, content-type")
        self.end_headers()

    def do_GET(self):
        # print "---"
        # print self.path
        if self.path == '/':
            self.do_header()
            self.wfile.write("ok")
        # print "---"

    def do_OPTIONS(self):
        # print "---"
        # print self.path
        if self.path == '/annotations':
            self.do_header()
            length = int(self.headers.getheader('content-length'))
            raw = self.rfile.read(length)
            # print raw
            data = json.loads(fixUnquotedParameters(raw))
            self.wfile.write(self._grafana.annotations(data))
        elif self.path == '/query':
            self.do_header()
            length = int(self.headers.getheader('content-length'))
            raw = self.rfile.read(length)
            # print raw
            data = json.loads(fixUnquotedParameters(raw))
            self.wfile.write(self._grafana.query(data))
        elif self.path == '/search':
            self.do_header()
            try:
                length = int(self.headers.getheader('content-length'))
                raw = self.rfile.read(length)
                # print raw
                data = json.loads(fixUnquotedParameters(raw))
            except:
                data = {}
            self.wfile.write(self._grafana.search(data))
        else:
            self.send_response(404)
            self.end_headers()
        # print "---"

    def do_POST(self):
        return self.do_OPTIONS()


def main():
    httpserver = BaseHTTPServer.HTTPServer((LOCAL_HOST, PORT_NUMBER),
                                           MyHandler)
    try:
        httpserver.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpserver.server_close()


if __name__ == '__main__':
    main()
