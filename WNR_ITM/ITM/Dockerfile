# Copyright 2022 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM openvino/ubuntu20_data_runtime:2021.4.2

USER root
WORKDIR /

RUN apt-get update && apt-get install -y git && apt-get clean all

RUN  git clone https://github.com/openvinotoolkit/open_model_zoo.git
WORKDIR /open_model_zoo/tools/downloader
RUN git checkout 2021.4.2

RUN /bin/bash -c 'python3 -mpip install --user -r ./requirements.in'
COPY app/models.lst /models.lst
RUN /bin/bash -c './downloader.py --list /models.lst -o /'

RUN apt-get autoremove -y git

USER openvino
WORKDIR /

COPY --chown=openvino:openvino app /app
WORKDIR /app
RUN pip3 install --upgrade pip
RUN pip3 install -r requirements.txt
RUN openssl genrsa -out itm-key.pem 2048
RUN openssl req -new -key itm-key.pem -out itm-csr.csr -subj "/C=US/ST=CA/L=SmartCity/O=Intel/OU=IT/CN=intel.com/emailAddress=intel@intel.com"
RUN openssl x509 -req -days 365 -in itm-csr.csr -signkey itm-key.pem -out itm.pem

CMD [ "/bin/bash", "run.sh" ]
