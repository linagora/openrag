## Configuring the Indexer UI

### 1. Download the `indexer-ui` Submodule
> Ensure the `indexer-ui` submodule is properly initialized and downloaded. Run the following command from the root of your `openrag` project:

```bash
cd <project-name> # openrag project
git submodule update --init --recursive
```
> [!IMPORTANT]
> To get the newest version of indexer ui, run the following command
```bash
git submodule foreach 'git checkout main && git pull'
```

> [!Note]
> The `--init --recursive` flags will:
>
> * Initialize all submodules defined in the `.gitmodules` file
> * Clone the content of each submodule
> * Recursively initialize and update any nested submodules

### 2. Set Environment Variables

To enable the Indexer UI, add the following environment variables to your configuration:

```bash
INDEXERUI_COMPOSE_FILE=extern/indexer-ui/docker-compose.yaml  # Required path to the docker-compose file
INDEXERUI_PORT=8067                                             # Port to expose the Indexer UI (default is 3042)
INDEXERUI_URL='http://X.X.X.X:INDEXERUI_PORT'                   # Base URL of the Indexer UI (required to prevent CORS issues)
VITE_API_BASE_URL='http://X.X.X.X:APP_PORT'                     # Base URL of your FastAPI backend. Used by the frondend

```

Make sure to replace `X.X.X.X`, `APP_PORT`, and `INDEXERUI_PORT` with your actual IP address and port values.

> ![!IMPORTANT]
> Make sure to comment out all of these variables if you do not intend to use this UI