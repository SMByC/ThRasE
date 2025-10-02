#!/usr/bin/env bash

set -e

pushd /usr/src
DEFAULT_PARAMS='./ThRasE/tests/ -v --qgis_disable_gui --qgis_disable_init'
xvfb-run -a -s "-screen 0 1920x1080x24" pytest ${@:-`echo $DEFAULT_PARAMS`}
popd
