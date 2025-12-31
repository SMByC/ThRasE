#!/usr/bin/env bash

set -e

cd /usr/src/ThRasE

DEFAULT_PARAMS='./tests/ -v --qgis_disable_gui --qgis_disable_init'
xvfb-run pytest ${@:-$DEFAULT_PARAMS}
