#!/usr/bin/env bash

source venv/bin/activate

docker container ls
OPENRAG_ADDR=`docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' openrag-openrag-cpu-1`
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' openrag-openrag-cpu-1

docker logs openrag-openrag-cpu-1

while true; do
  STATUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${OPENRAG_ADDR}:8080/health_check")
  if [ "$STATUS_CODE" -eq 200 ]; then
    echo "$(date): API is up and running"
    break
  else
    echo "$(date): Health check failed with status $STATUS_CODE, retrying..."
    sleep 10
fi
done

