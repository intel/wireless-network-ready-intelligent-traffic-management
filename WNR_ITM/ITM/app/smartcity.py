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

import sys
import time
import json
import math
import logging
from queue import Queue
from argparse import ArgumentParser
from gstgva import VideoFrame, util
import cv2
import influxdb
import gi
from gi.repository import Gst
import yolo_labels
from utils import Point, Rect
from tracker import SingleTracker, TrackingManager, TrackingSystem, InfluxDB

gi.require_version('GObject', '2.0')
gi.require_version('Gst', '1.0')

log = logging.getLogger(__name__)
tracking_system = []
TRACKING = True
COLLISION = True


class FpsManager:
    """
    Class to calculate FPS for each stream
    """
    def __init__(self, num_ch):
        self.num_ch = num_ch
        self.st_time = [0]*num_ch
        self.frame_counts = [0]*num_ch

    def update_ch(self, ch_id):
        """
        Update and return average FPS for channel <ch_id>
        Average FPS is used because it is stable and don't fluctuate.
        """
        if self.st_time[ch_id] == 0:
            self.st_time[ch_id] = time.monotonic()

        self.frame_counts[ch_id] += 1
        t = time.monotonic()
        fps = round(self.frame_counts[ch_id]/(t - self.st_time[ch_id]), 2)
        return fps


def frame_callback(frame: VideoFrame, conf_data, fps_manager, ch_id, q_data, running):
    """
    Frame callback function. Draw bounding boxes, track and detects collision.
    :param frame: VideoFrame object
    :param conf_data: Configuration from configuration file
    :param fps_manager: Object of FpsManager class, to calculate FPS
    :param ch_id: Channel ID
    :param q_data: Interprocess dictionary, where keys are channel ids and values are multiprocess queues (SimpleQueue)
    :param running: Interprocess list. Indicator for populating queues
    """
    fps = fps_manager.update_ch(ch_id)
    scale, thickness, font = 0.7, 2, cv2.FONT_HERSHEY_SIMPLEX
    first_results = []
    width = frame.video_info().width
    height = frame.video_info().height
    with frame.data() as mat:
        text = f'FPS: {fps}'
        (text_width, text_height) = cv2.getTextSize(text, font, scale, thickness)[0]
        offset_x, offset_y = 10, 20
        box_coords = ((offset_x, offset_y), (offset_x + text_width + 2, offset_y - text_height - 2))
        cv2.rectangle(mat, box_coords[0], box_coords[1], (255, 255, 255), cv2.FILLED)
        cv2.putText(mat, text, (offset_x, offset_y), font, scale, (0, 0, 0), 1)
        for roi in frame.regions():
            if roi.confidence() < 0.5:
                continue
            rect = roi.rect()
            label = roi.label_id()
            if label == 1 and 'pedestrian' in conf_data[ch_id]['analytics']:
                label = yolo_labels.LABEL_PERSON
                color = yolo_labels.COLOR_PERSON
            elif label == 0 and 'vehicle' in conf_data[ch_id]['analytics']:
                label = yolo_labels.LABEL_CAR
                color = yolo_labels.COLOR_CAR
            elif label == 2 and 'bike' in conf_data[ch_id]['analytics']:
                label = yolo_labels.LABEL_BICYCLE
                color = yolo_labels.COLOR_BIKE
            else:
                continue

            if TRACKING:
                first_results.append((Rect(rect.x, rect.y, rect.w, rect.h), label))
            else:
                cv2.rectangle(mat, (int(rect.x), int(rect.y)),
                              (int(rect.x + rect.w), int(rect.y + rect.h)),
                              (0, 255, 255), 2)
                text = yolo_labels.get_label_str(label)
                (text_width, text_height) = cv2.getTextSize(text, font, scale, thickness)[0]
                box_coords = ((rect.x, rect.y), (rect.x + text_width + 2, rect.y - text_height - 2))
                cv2.rectangle(mat, box_coords[0], box_coords[1], (0, 255, 255), cv2.FILLED)
                cv2.putText(mat, text, (rect.x, rect.y), font, scale, (0, 0, 0), 1)

        if TRACKING:
            if not tracking_system[ch_id].is_initialized:
                tracking_system[ch_id].init_tracker_system(width, height, first_results, len(conf_data))
            tracking_system[ch_id].update_tracking_system(first_results)
            tracking_success = tracking_system[ch_id].start_tracking(mat)
            if not tracking_success:
                log.error('Tracking failed')
                sys.exit(-1)
            if (tracking_system[ch_id].manager.tracker_vec) != 0:
                if COLLISION and ('vehicle' in conf_data[ch_id]['analytics'] or 'bike' in conf_data[ch_id]['analytics']):
                    tracking_system[ch_id].detect_collision()
                tracking_system[ch_id].draw_tracking_results(mat)
        try:
            if running[ch_id]:
                q_data[ch_id].put(mat)
            else:
                time.sleep(0.005)
        except FileNotFoundError:
            sys.exit()


def pad_probe_callback(pad, info, conf_data, fps_manager, ch_id, q_data, running):
    """
    Set callback
    """
    with util.GST_PAD_PROBE_INFO_BUFFER(info) as buffer:
        caps = pad.get_current_caps()
        frame = VideoFrame(buffer, caps=caps)
        frame_callback(frame, conf_data, fps_manager, ch_id, q_data, running)
    return Gst.PadProbeReturn.OK


def create_launch_string(conf_data, vp_model, vp_proc, show_output):
    """
    Create gstreamer pipeline
    """
    width, height = 640, 320
    num_ch = len(conf_data)
    pipeline = ''
    for i, conf in enumerate(conf_data):
        if '/dev/video' in conf['path']:
            source = "v4l2src device"
        elif '://' in conf['path']:
            source = "urisourcebin buffer-size=4096 uri"
        else:
            source = "filesrc location"

        pipeline += f"{source}=\"{conf['path']}\" ! decodebin ! videoconvert n-threads=4 ! videoscale n-threads=4 " \
                    f"! video/x-raw,format=BGR,width={width},height={height} " \
                    f"! gvadetect name={'gvadetect'+str(i)} model=\"{vp_model}\" model_proc=\"{vp_proc}\" device={conf['device']} "
        if show_output:
            pipeline += f"! queue  leaky=downstream max-size-buffers=4294967295 max-size-bytes=4294967295 " \
                        f" max-size-time=100000000000 name={'queue'+str(i)} ! m.sink_{i} "
    if show_output:
        pipeline += f" videomixer name=m "
        num_rows = int(math.ceil(math.sqrt(num_ch)))
        num_cols = int((num_ch-1) / num_rows) + 1
        i = 0
        for c in range(num_cols):
            for r in range(num_rows):
                if i == num_ch: break
                x, y = int(r*width), int(c*height)
                pipeline += f"sink_{i}::xpos={x} sink_{i}::ypos={y} "
                i += 1
        pipeline += f"! video/x-raw,width={width*num_rows},height={height*num_cols} ! videoscale "
        sink ="! autovideosink"
    else:
        sink = "! fakesink sync=false"
    pipeline += sink
    return pipeline


def set_callbacks(pipeline, conf_data, q_data, running):
    """
    Set callback for each channel
    """
    num_ch = len(conf_data)
    fps_manager = FpsManager(num_ch)
    for ch_id in range(num_ch):
        gvadetect = pipeline.get_by_name('gvadetect'+str(ch_id))
        pad = gvadetect.get_static_pad('src')
        pad.add_probe(Gst.PadProbeType.BUFFER, pad_probe_callback, conf_data, fps_manager, ch_id, q_data, running)


def start_app(config_data, vp_model, vp_proc, is_tracking, is_collsion,
              client, q_data, running, show_output=False):
    """
    Main function to start smart city.
    """
    global TRACKING, COLLISION
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s :: %(message)s")
    TRACKING, COLLISION = is_tracking, is_collsion
    num_ch = len(config_data)
    client = InfluxDB(client, num_ch)
    client.start()
    for i in range(num_ch):
        tracking_system.append(TrackingSystem(i, client, config_data[i]))
    Gst.init(sys.argv)
    gst_launch_string = create_launch_string(config_data, vp_model,
                                             vp_proc, show_output)
    log.info(f'\nPipleine::\n\n{gst_launch_string}\n\n')
    while True:
        try:
            pipeline = Gst.parse_launch(gst_launch_string)
            set_callbacks(pipeline, config_data, q_data, running)
            log.info('Pipeline started..')
            pipeline.set_state(Gst.State.PLAYING)
            bus = pipeline.get_bus()
            msg = bus.timed_pop_filtered(
                Gst.CLOCK_TIME_NONE, Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg.type != Gst.MessageType.EOS:
                err, debug = msg.parse_error()
                log.error(f'Error: {err}\nAdditional debug info: {debug}')
            pipeline.set_state(Gst.State.NULL)
            log.info('Pipeline completed. Loop again...')
        except KeyboardInterrupt:
            break
    pipeline.set_state(Gst.State.NULL)
    client.stop()


