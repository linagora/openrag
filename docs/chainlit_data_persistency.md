# Data Persistency

The [Chainlit data layer](https://docs.chainlit.io/data-layers/overview) allows you to persist conversations in chainlit.
This project uses a [dockerized fork](https://github.com/Chainlit/chainlit-datalayer) for easier deployment and setup.

In OpenRAG, one can activate **`Chainlit data layer`** following these steps:

### Step 1: Set up authentication
In fact, chainlit authentication is necessary for data persistency. Set chainlit authentication if not already done (refer to the [chainlit auth guide](./setup_chainlit_ui_auth.md))

### Step 2: Add the following variables
To deploy the Chainlit data layer service, add the following variable:
```bash
# Persistency services: postgres (localstack (AWS emulator deployed locally)
CHAINLIT_DATALAYER_COMPOSE=extern/chainlit-datalayer/compose.yaml
```
This provides 2 services:
- a postgres database to store users, feedbacks, chat history, etc
- "s3 bucket" emulator to store elements (files attached in the chat). 
> [!NOTE]
> Chainlit datalayer is cloud-compatible, and the same applies for local data persistency. So for local storage, a cloud/s3 service emulator that runs in a container is deployed as well.

* Variables for the postgres data
> [!IMPORTANT]
> Knowing that OpenRAG already has a running postgres service (**`rdb`**) (refer to the [docker-compose.yaml](../docker-compose.yaml) file), there is no need to deploy another postgres service. In that case, comment out the postgres service definition in the [compose.yaml file](../extern/chainlit-datalayer/compose.yaml) and add the following variable to your .env

```bash
DATABASE_URL=postgresql://root:root_password@rdb:5432/chainlit
```
* Variables for chainlit to use the **`S3 Bucket`**
Add the following variables to your `.env` so that chainlit can use them to connect to the locally deployed S3 bucket

```bash
## S3 bucket configuration.
BUCKET_NAME=my-bucket
APP_AWS_ACCESS_KEY=random-key
APP_AWS_SECRET_KEY=random-key
APP_AWS_REGION=eu-central-1
DEV_AWS_ENDPOINT=http://localstack:4566
```

> [!IMPORTANT]  
> If you want to deactivate the service, comment out these variables, especially `CHAINLIT_DATALAYER_COMPOSE`.