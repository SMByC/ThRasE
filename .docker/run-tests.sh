#!/bin/bash

export GITHUB_WORKSPACE=$PWD
docker compose -f .docker/docker-compose.gh.yml run qgis /usr/src/ThRasE/.docker/run-docker-tests.sh $@
docker compose -f .docker/docker-compose.gh.yml rm -s -f
