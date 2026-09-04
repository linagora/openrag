---
title: Grafana HTTP dashboard
description: Build, understand, and maintain the OpenRAG HTTP dashboard.
---

# Grafana HTTP dashboard

The OpenRAG HTTP dashboard starts with a service-level health summary and progressively exposes more detail. Operators can see whether the service needs attention without understanding Prometheus, while developers can filter traffic by endpoint and HTTP method when investigating a problem.

## Before opening the dashboard

Start OpenRAG with its monitoring overlay from `infra/compose`:

```bash
export SHARED_ENV="$PWD/.env"
docker compose --env-file .env \
  -f docker-compose.yaml \
  -f monitoring.docker-compose.yaml \
  up -d
```

Grafana must be reachable from the browser. For a remote deployment, `GRAFANA_URL` and `GF_SERVER_ROOT_URL` must use the public hostname rather than `localhost`. `localhost` is appropriate only when the browser runs on the server or reaches it through an SSH tunnel.

Confirm that Prometheus and Grafana are running:

```bash
docker compose \
  -f docker-compose.yaml \
  -f monitoring.docker-compose.yaml \
  ps prometheus grafana
```

Open Grafana through the URL configured in `GRAFANA_URL` and sign in with `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD`.

## Check the Prometheus data source

Open **Connections → Data sources → Prometheus**. The bundled data source uses `http://prometheus:9090` and the UID `prometheus`.

Open **Explore**, select Prometheus, and run:

```promql
openrag_http_requests_total
```

A working query returns series with `method`, `endpoint`, and `status_code` labels. If it returns no data, send a few requests to OpenRAG and wait for the next Prometheus scrape.

## How to edit a panel

Open the dashboard, hover over a panel, select its menu, and choose **Edit**. The preview occupies the top of the editor, queries appear at the bottom, and visualization settings appear in the right sidebar.

For a Prometheus query:

1. Select the **Query** tab and the Prometheus data source.
2. Find query **A** and switch from **Builder** to **Code**.
3. Paste the PromQL expression and select **Run queries**.
4. Choose the visualization in the right sidebar.
5. Set the title and description under **Panel options**.
6. Set the unit and decimals under **Standard options**.
7. For a Stat panel, select **Calculate** and **Last not null** under **Value options**.
8. Select **Apply**, then save the dashboard.

If the **Code** button is hidden, widen the window, reduce the browser zoom, or open the query row's menu and select **Switch to code**.

## Dashboard organization

### OpenRAG Health

The first row answers four immediate questions:

- **Requests/sec:** Is OpenRAG receiving traffic?
- **Error rate:** What percentage of requests are failing?
- **P95 latency:** How long do 95% of requests take?
- **Service status:** Can Prometheus reach OpenRAG?

The Error rate and P95 latency cards use green, yellow, and red thresholds. Their thresholds are operational defaults, not universal service-level objectives; deployments should adjust them to match expected workloads.

To configure the Error rate Stat panel, use **Percent (0-100)** as the unit, one decimal place, and **Last not null** as the calculation. Under **Thresholds**, choose **Absolute** mode and configure:

| Starting value | Color | Meaning |
| --- | --- | --- |
| Base | Green | Below 1% |
| 1 | Yellow | Between 1% and 5% |
| 5 | Red | At least 5% |

The query already returns a percentage between 0 and 100, so do not use the `Percent (0.0-1.0)` unit.

### HTTP Trends

The trends row shows traffic grouped by method, separates 4xx client errors from 5xx server errors, and places p50, p95, and p99 latency on one chart. Keeping the percentiles together makes the gap between typical and worst-case behavior visible.

### Problem Endpoints

The problem row ranks the five routes with the highest error percentage and the five routes with the highest p95 latency. These are current rankings, so their queries run as instant queries and use horizontal bar gauges.

### Endpoint Diagnostics

The final row uses the **Endpoint** and **Method** selectors at the top of the dashboard. It shows filtered request rate, status-code traffic, and a latency-distribution heatmap. Select **All** to restore a service-wide view.

## Dashboard variables

Variables are configured under **Dashboard settings → Variables**. Both variables use Prometheus, allow multiple values, include an **All** option, and use `.*` as the custom All value.

The Endpoint variable reads:

```promql
label_values(openrag_http_requests_total, endpoint)
```

The Method variable reads:

```promql
label_values(openrag_http_requests_total, method)
```

Filtered queries match the selections with regular expressions:

```promql
{endpoint=~"$endpoint", method=~"$method"}
```

## Persist changes in the repository

The bundled dashboard is provisioned from `infra/compose/grafana/dashboards/openrag-http.json`. A change saved only in Grafana can be lost when the container is recreated.

After testing a dashboard copy, open **Dashboard settings → JSON model** or **Share → Export**, export the JSON without the external-sharing option, and replace the provisioned file. Preserve the UID `openrag-http`; the Admin UI link depends on it.

Validate the file before restarting Grafana:

```bash
jq empty infra/compose/grafana/dashboards/openrag-http.json
docker restart openrag-grafana
docker logs --tail 50 openrag-grafana
```

After Grafana restarts, refresh the dashboard and confirm that both selectors and all four rows are present.

## Common problems

**The page returns 502 Bad Gateway.** The Admin UI proxy cannot reach the Grafana container. Confirm that Grafana is running and attached to the same Compose network.

**A panel shows No data.** Confirm the selected time range, generate OpenRAG traffic, check the Endpoint and Method selectors, and run the query in Explore.

**Service status shows Unknown.** Run `up` in Explore and inspect the `job` label. If it is not `openrag`, update the Service status query with the actual job name.

**Dashboard edits disappear.** Export the tested dashboard and update the provisioned JSON file instead of relying on Grafana's internal database.
