# Data Persistency

Chainlit data layer can be activated using a git submodule:  
https://github.com/OpenLLM-France/chainlit-datalayer/tree/main (forked from https://github.com/Chainlit/chainlit-datalayer).  
With the fork, we industrialized the repo by dockerizing it.

- It deploys a PostgreSQL database for data persistency.
- The implementation by Chainlit is cloud-compatible, and the same applies for local data persistency.
- A local "fake" S3 bucket is deployed for local storage.

## Usage in Our App

### Cloning with Submodules

If you want to have everything at once, clone the project with submodule dependencies:

```bash
git clone --recurse-submodules <repository-url>
```

If you've already cloned this repo without submodules:

```bash
cd <project-name>
git submodule update --init --recursive
```

> [!IMPORTANT]
> To get the newest version of chainlit datalayer, run the following command
```bash
git submodule foreach 'git checkout main && git pull'
```

[!NOTE]  
The `--init --recursive` flags will:
- Initialize the submodules based on the existing `.gitmodules` file
- Download all the submodule content
- Handle nested submodules if any exist

### Environment Variables

After adding the submodule, add the following variables to your `.env` file:

```bash
# Chainlit UI authentication (Necessary for data persistency). Don't add it if it's already included
CHAINLIT_AUTH_SECRET=... # has to be generated with with this command: 'uv run chainlit create-secret' but a random value works too.
CHAINLIT_USERNAME=OpenRAG
CHAINLIT_PASSWORD=OpenRAG2025

## Chainlit data persistency
# Persistency services (localstack + AWS (Deployed Locally))
CHAINLIT_DATALAYER_COMPOSE=extern/chainlit-datalayer/compose.yaml

## To link to the PostgreSQL instance.
POSTGRES_USER=root
POSTGRES_PASSWORD=root
POSTGRES_DB=postgres
POSTGRES_PORT=5432

DATABASE_URL=postgresql://${POSTGRES_USER:-root}:${POSTGRES_PASSWORD:-root}@postgres:${POSTGRES_PORT:-5432}/${POSTGRES_DB:-postgres} # for chainlit

## S3 configuration.
BUCKET_NAME=my-bucket
APP_AWS_ACCESS_KEY=random-key
APP_AWS_SECRET_KEY=random-key
APP_AWS_REGION=eu-central-1

LOCALSTACK_PORT=4566
DEV_AWS_ENDPOINT=http://localstack:${LOCALSTACK_PORT:-4566}
```

>[!IMPORTANT]  
>If you want to deactivate the service, comment out these variables, especially `CHAINLIT_DATALAYER_COMPOSE`.