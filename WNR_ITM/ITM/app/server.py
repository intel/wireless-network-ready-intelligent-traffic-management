"""
Copyright 2022 Intel Corporation

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import os
import math
import sys
import time
import json
import logging
import re
from argparse import ArgumentParser
from multiprocessing import Process, Manager, SimpleQueue, Lock
from flask import Flask, Response, jsonify, render_template, make_response
from openvino.inference_engine import IECore
import requests
import influxdb
import numpy as np
import cv2
import smartcity
import validate_config

app = Flask(__name__)
log = logging.getLogger(__name__)

SERVER_HOST = os.getenv('LOCAL_HOST')
NAMESPACE = os.getenv('NAMESPACE')
GRAFANA_HOST = os.getenv("GRAFANA_HOST")
GRAFANA_PASSWORD = os.getenv("GRAFANA_PASSWORD")
GRAFANA_PORT = "3000"
HOST_IP = os.getenv("HOST_IP")
SERVER_PORT = os.getenv("SERVER_PORT")
INFLUXDB_HOST = "influxdb.{}.svc".format(NAMESPACE)
INFLUXDB_PORT = "8086"

MAP_JS_CDN = "https://cdn.jsdelivr.net/gh/openlayers/openlayers.github.io@master/en/v6.4.3/build/ol.js"
JS_CDN_INTEGRITY = "sha384-RffttofZaGGmE3uVvQmIW/dh1bzuHAJtWkxFyjRkb7eaUWfHo3W3GV8dcET2xTPI"
MAP_CSS_CDN = "https://cdn.jsdelivr.net/gh/openlayers/openlayers.github.io@master/en/v6.4.3/css/ol.css"

FPS = 20
NUM_CH = 1
RUNNING = []
MUTEX = Lock()
CONFIG_PATH = None
CONF_DATA, URL_DATA = {}, {}
Q_DATA = {}
CURRENT_FRAMES = []

class GrafanaConnect:
    """
    Class to communicate with grafana server
    """
    def __init__(self, grafana_url, map_server_url, influxdb_url, user, password):
        """
        Init function
        """
        self.grafana_url = grafana_url
        self.map_server_url = map_server_url
        self.influxdb_url = influxdb_url
        self.datasource_url = os.path.join(self.grafana_url, 'api/datasources')
        self.dashboard_url = os.path.join(self.grafana_url, 'api/dashboards/db')
        self.datasource_search_url = os.path.join(self.grafana_url, 'api/search')
        self.auth = requests.auth.HTTPBasicAuth(user, password)
        self.headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Cache-Control': 'no-cache'
        }

    def _post(self, url, json_data):
        """
        Post data to grafana server
        """
        try:
            r = requests.post(url, auth=self.auth,
                              headers=self.headers,
                              json=json_data)
            return r
        except Exception as err:
            log.error(f'Error: {err}')
            return -1

    def _get(self, url, timeout=None):
        """
        Get data from grafana server
        """
        try:
            r = requests.get(url, auth=self.auth, headers=self.headers, timeout=timeout)
            return r
        except Exception as err:
            return -1

    def create_datasource(self, template_path):
        """
        Add/Update datasource
        """
        with open(template_path, 'r') as f:
            json_data = json.loads(f.read())
        json_data["url"] = self.influxdb_url
        r = self._post(self.datasource_url, json_data=json_data)
        if r == -1:
            log.error('Failed to connect to grafana container.')
            sys.exit(-1)
        res = r.json()
        if 'Data source with the same name already exists' in res['message']:
            pass
        elif 'Datasource added' in res['message']:
            test_query = self.datasource_url + \
                         f'/proxy/{res["id"]}/query?db={json_data["database"]}' \
                         f'&q=SHOW%20RETENTION%20POLICIES%20on%20"{json_data["database"]}"&epoch=ms'
            r = self._get(test_query)
            if r.json()['results'][0]['statement_id'] == 0:
                log.info('Data added successfully')
            else:
                log.warning('Failed to add datasource')
        return res

    def add_dashboard(self, json_data, url=None):
        """
        Add/Update dashboard
        """
        if url:
            json_data['dashboard']['panels'][1]['url'] = self.map_server_url + url
        else:
            json_data['dashboard']['panels'][1]['url'] = self.map_server_url + '/dashboard'
        r = self._post(self.dashboard_url, json_data=json_data)
        try:
            res = r.json()
            log.info(f'Successfully added dashboard {res["id"]}')
        except:
            log.error(f'Error in updating dashboard. Message: {r}')
        return res

    def add_channel_dashbords(self, template_path, camera_conf):
        """
        Add/Update dashboards for each channel
        """
        url_data = {}
        with open(template_path, 'r') as f:
            str_data = f.read()
        for i in range(0, NUM_CH):
            st = re.sub("channel0", f'channel{i}', str_data)
            final_data = json.loads(st)
            final_data['dashboard']['title'] = f'ITM - {camera_conf["cameras"][i]["address"]}'
            final_data['dashboard']['panels'][2]['url'] = self.map_server_url + f'/camera/{i}'
            final_data['dashboard']['panels'][2]['method'] = "iframe"
            res = self.add_dashboard(final_data, f'/camera/{i}')
            url_data[i] = GRAFANA_EXTERNAL_URL + res['url']
        return url_data

    def init_grafana_server(self, camera_config, datasource_template_path,
                            consolidated_dashboard_template_path,
                            channel_dashboard_template_path):
        """
        Initialize datasource and dashboards on grafana server
        """
        # test grafana api
        i = -1
        while i < 100:
            i += 1
            time.sleep(1)
            test = self._get(self.datasource_url, timeout=3)
            if test == -1 or test.status_code != 200:
                log.info('Connecting grafana: Grafana container not up yet, retrying...')
                continue
            else:
                break
        self.create_datasource(datasource_template_path)
        with open(consolidated_dashboard_template_path, 'r') as f:
            json_data = json.loads(f.read())
        res = self.add_dashboard(json_data)
        url_data = self.add_channel_dashbords(channel_dashboard_template_path,
                                              camera_config)
        url_data[-1] = GRAFANA_EXTERNAL_URL + res['url']
        return url_data


def _stream_channel(cam_id):
    """
    Generator.
    Yield frames that belongs to <cam_id>.
    """
    global Q_DATA, RUNNING
    MUTEX.acquire()
    RUNNING[cam_id] = True
    MUTEX.release()
    q = Q_DATA[cam_id]
    max_try = 4000
    try:
        while True:
            if q.empty() and max_try > 0:
                MUTEX.acquire()
                RUNNING[cam_id] = True
                MUTEX.release()
                max_try -= 1
                time.sleep(0.01)
                continue
            elif q.empty() and max_try <= 0:
                log.error('Unable to recevie frames from pipeline, Unknown error.')
                break
            max_try = 4000
            frame = q.get()
            CURRENT_FRAMES[cam_id] = frame
            ret, frame = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            time.sleep(1/FPS)
            yield (b' --frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' +
                   frame.tobytes() + b'\r\n\r\n')
    except Exception as err:
        log.error(f'Error: {err}')
    finally:
        MUTEX.acquire()
        RUNNING[cam_id] = False
        MUTEX.release()
        CURRENT_FRAMES[cam_id] = None
        # empty the queue
        while not q.empty():
            _ = q.get()


def _get_all_streams(num_ch):
    """
    Generator.
    Combine and yield frames from all running video streams.
    """
    global Q_DATA, CURRENT_FRAMES, RUNNING
    height, width = 320, 640
    num_rows = math.floor(math.sqrt(num_ch+1))
    num_cols = math.ceil(num_ch/num_rows)
    idx = 0
    MUTEX.acquire()
    for i in range(num_ch):
        RUNNING[i] = True
    MUTEX.release()
    base = np.zeros((height*num_rows, width*num_cols, 3), np.uint8)
    try:
        while True:
            for idx in range(num_ch):
                if idx >= num_ch:
                    break
                if CURRENT_FRAMES[idx] is None:
                    if Q_DATA[idx].empty():
                        time.sleep(0.01)
                        continue
                    frame = Q_DATA[idx].get()
                else:
                    frame = CURRENT_FRAMES[idx]
                x = int(width * int(idx % num_cols))
                y = int(height * int(idx / num_cols))
                base[y : y + height, x : x + width] = frame
            ret, base_en = cv2.imencode('.jpg', base)
            if not ret:
                continue
            time.sleep(1/FPS)
            yield (b' --frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' +
                   base_en.tobytes() + b'\r\n\r\n')
    except Exception as err:
        log.error(f'Error: {err}')
    finally:
        for idx in range(num_ch):
            if CURRENT_FRAMES[idx] is None:
                MUTEX.acquire()
                RUNNING[idx] = False
                MUTEX.release()
                while not Q_DATA[idx].empty():
                    _ = Q_DATA[idx].get()


@app.route('/get_all_streams')
def get_all_streams():
    """
    Route to show all running video streams
    """
    return Response(_get_all_streams(NUM_CH),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@app.route('/camera/<cam_id>')
def open_stream(cam_id):
    """
    Route to individual video stream identified by <cam_id>.
    If <cam_id> is 'all' render HTML that shows all video streams.
    Calls _stream_channel(cam_id) function.
    """
    try:
        if not cam_id.isnumeric():
            return Response("The URL does not exist", 401)
        cam_id = int(cam_id)
        if cam_id >= NUM_CH:
            return Response("The URL does not exist", 401)
        return Response(_stream_channel(cam_id),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    except Exception as err:
        log.error(f'Error: {err}')


@app.route('/dashboard')
def dashboard():
    """
    Route to HTML page which shows MapUI. Home Page.
    """
    conf = CONF_DATA
    conf['urls'] = URL_DATA
    response = make_response(render_template('dashboard.html', title='Dashboard',
                             map_js=MAP_JS_CDN, map_cdn_integrity=JS_CDN_INTEGRITY,
                             map_css=MAP_CSS_CDN, config=json.dumps(conf)))
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response


@app.after_request
def add_csp(resp):
    resp.headers['Content-Security-Policy']  =  "frame-ancestors 'none' https://*:32000 ;" \
                                                "media-src 'none' ; " \
                                                "object-src 'none' ; " \
                                                "connect-src 'none' ; " \
                                                "plugin-src 'none' ; " \
                                                "frame-src 'none' ; " \
                                                "img-src 'self' https://openlayers.org http://a.tile.openstreetmap.org http://b.tile.openstreetmap.org http://c.tile.openstreetmap.org ; "
    return resp


def init_all(over_write=False):
    """
    Initialize global variables and
    update datasources and dashboards on grafana server
    """
    global NUM_CH, CONF_DATA, URL_DATA
    try:
        num_ch, conf_data, given_devices  = validate_config.read_config(f"{CONFIG_PATH}")
    except validate_config.ConfigException as err:
        log.error(str(err))
        sys.exit(-1)
    for cam_detail in conf_data['cameras']:
        cap = cv2.VideoCapture(cam_detail['path'])
        ret, _ = cap.read()
        if not ret or not cap.isOpened():
            log.error(f'Unable to open source - `{cam_detail["path"]}`')
            sys.exit(-1)
        cap.release()
    ie = IECore()
    for device in given_devices:
        if device not in ie.available_devices:
            log.error(f'Device not found - `{device}`. '
                      f'All available devices - {ie.available_devices}.')
            sys.exit(-1)
    if not over_write or conf_data == CONF_DATA:
        return
    NUM_CH, CONF_DATA = num_ch, conf_data
    grafana_connect = GrafanaConnect(GRAFANA_URL, MAP_SERVER_URL, INFLUXDB_URL, 'admin', GRAFANA_PASSWORD)
    URL_DATA = grafana_connect.init_grafana_server(CONF_DATA, 'grafana_templates/datasource_template.json',
                                                   'grafana_templates/consolidated_dashboard_template.json',
                                                   'grafana_templates/channel_dashboard_template.json')
    if URL_DATA == -1:
        sys.exit(-1)


def check_args(args):
    """
    Check arguments
    """
    this_path = os.path.dirname(__file__)
    if not args.config_path.startswith(this_path):
        config_path = os.path.join(this_path, f"{args.config_path}")
    else:
        config_path = f"{args.config_path}"
    if not os.path.isfile(config_path):
        log.error(f'Config file `{config_path}`does not exist. ')
        sys.exit(-1)
    if not args.vp_model.startswith(this_path):
        vp_model = os.path.join(this_path, f"{args.vp_model}")
    else:
        vp_model = f"{args.vp_model}"
    if not os.path.isfile(vp_model):
        log.error(f'Model `{vp_model}`does not exist. ')
        sys.exit(-1)
    if not args.vp_proc.startswith(this_path):
        vp_proc = os.path.join(this_path, f"{args.vp_proc}")
    else:
        vp_proc = f"{args.vp_proc}"
    if not os.path.isfile(vp_proc):
        log.error(f'Model proc file `{vp_proc}`does not exist. ')
        sys.exit(-1)
    try:
        _ = validate_config.read_model_proc(vp_proc)
    except validate_config.ConfigException as err:
        lof.error(str(err))
        sys.exit(-1)


def main():
    """
    Main Function
    """
    global GRAFANA_URL, MAP_SERVER_URL, INFLUXDB_URL, CONFIG_PATH, Q_DATA, RUNNING, CURRENT_FRAMES, GRAFANA_EXTERNAL_URL
    parser = ArgumentParser()
    parser.add_argument("-c", "--config_path",
                        help="Path to camera config file",
                        required=True, type=str)
    parser.add_argument("-vp_model", "--vp_model",
                        help="Path to model file",
                        required=True, type=str)
    parser.add_argument("-vp_proc", "--vp_proc",
                        help="Path to model proc file",
                        required=True, type=str)
    parser.add_argument("--tracking", action="store_true",
                        help="Optional. To track the detection or not.",
                        required=False, default=True)
    parser.add_argument("--detect_collision", action="store_true",
                        help="Optional. To detect collision or not.",
                        required=False, default=True)
    parser.add_argument("-g_host", "--grafana_host",
                        help="Grafana Host", default=GRAFANA_HOST,
                        required=False, type=str)
    parser.add_argument("-g_port", "--grafana_port",
                        help="Grafana Port", default=GRAFANA_PORT,
                        required=False, type=int)
    parser.add_argument("-infuxdb_h", "--influxdb_host",
                        help="Host IP of influxdb",
                        required=False, default=INFLUXDB_HOST, type=str)
    parser.add_argument("-infuxdb_p", "--influxdb_port",
                        help="Port of influxdb",
                        required=False, default=INFLUXDB_PORT, type=int)
    parser.add_argument("-influxdb_user", "--influxdb_username",
                        help="Username for of influxdb",
                        required=False, default="admin", type=str)
    parser.add_argument("-influxdb_pass", "--influxdb_password",
                        help="Password for of influxdb",
                        required=False, default="admin", type=str)
    parser.add_argument("-database", "--influxdb_database",
                        help="Database name for of influxdb",
                        required=False, default="itm_metadata", type=str)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s :: %(message)s")
    check_args(args)
    try:
        client = influxdb.InfluxDBClient(host=args.influxdb_host, port=args.influxdb_port,
                                         username=args.influxdb_username,
                                         password=args.influxdb_password,
                                         database=args.influxdb_database)
        # test and retry connecting influxdb
        i = -1
        while i<=20:
            i += 1
            try:
                client.drop_database(args.influxdb_database)
                break
            except:
                log.info('Retrying...')
                time.sleep(1)
        client.drop_database(args.influxdb_database)
        client.create_database(args.influxdb_database)
    except influxdb.exceptions.InfluxDBClientError as err:
        log.error(f'Can\'t connect to InluxDB. \n{err}')
        sys.exit(-1)
    except influxdb.exceptions.InfluxDBServerError as err:
        log.error(f'InfluxDB Server Error.\n{err}')
        sys.exit(-1)
    except Exception as err:
        log.error(f'Error: Failed to connect to Influxdb container.\nDebug Info: {err}')
        sys.exit(-1)

    GRAFANA_URL = f'http://{args.grafana_host}:{args.grafana_port}'
    INFLUXDB_URL = f'http://{args.influxdb_host}:{args.influxdb_port}'
    MAP_SERVER_URL = f'https://{HOST_IP}:{SERVER_PORT}'
    GRAFANA_EXTERNAL_URL = f'https://{HOST_IP}:32000'
    log.info("MAP SERVER URL %s " % MAP_SERVER_URL)
    log.info("GRAFANA_URL %s " % GRAFANA_URL)
    log.info("GRAFANA_EXTERNAL_URL %s" % GRAFANA_EXTERNAL_URL)
    CONFIG_PATH = args.config_path

    init_all(over_write=True)
    manager = Manager()
    RUNNING = manager.list([False]*NUM_CH)
    Q_DATA = {key:SimpleQueue() for key in range(0, NUM_CH)}
    CURRENT_FRAMES = [None]*NUM_CH
    tracking = args.tracking or args.detect_collision
    collision = args.detect_collision
    try:
        # Start smart city analytics in separate process
        process = Process(target=smartcity.start_app, args=(CONF_DATA['cameras'],
                          args.vp_model, args.vp_proc, tracking,
                          collision, client, Q_DATA, RUNNING))
        process.start()
        for c in CONF_DATA['cameras']:
            _ = c.pop("path")
            _ = c.pop("device")
            _ = c.pop("analytics")
        app.run(host=SERVER_HOST, port=8000, threaded=True, ssl_context=('itm.pem', 'itm-key.pem')) #Ignore bandit issue - [B104:hardcoded_bind_all_interfaces]
    except KeyboardInterrupt:
        process.terminate()


if __name__=='__main__':
    main()
