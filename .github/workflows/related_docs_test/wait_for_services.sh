#!/usr/bin/env bash

source venv/bin/activate

SERVICE_NAME=openrag-openrag-cpu-1
docker container ls
OPENRAG_ADDR=`docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${SERVICE_NAME}`
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' ${SERVICE_NAME}

docker logs ${SERVICE_NAME}

while true; do
  STATUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${OPENRAG_ADDR}:8080/health_check")
  if [ "$STATUS_CODE" -eq 200 ]; then
    echo "$(date): API is up and running"
    break
  else
    echo "$(date): ${SERVICE_NAME} Health check failed with status $STATUS_CODE, retrying..."
    docker logs ${SERVICE_NAME}
    sleep 10
fi
done

