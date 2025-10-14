---
title: Chainlit Authentification
---
To configure password-based authentication for your Chainlit UI, add the following environment variables to your `.env` file:
## Step 1: Set up the authentication secret

First, define a **`CHAINLIT_AUTH_SECRET`** environment variable. You can generate one automatically using the command `chainlit create-secret` (or `uv run chainlit create-secret` if using uv). Alternatively, you can provide your own **custom value**.

For detailed information about this variable, see the [Chainlit authentication documentation](https://docs.chainlit.io/authentication/overview).

## Step 2: Configure username and password

For password-based authentication (see [Chainlit password authentication docs](https://docs.chainlit.io/authentication/password)), add your desired username and password to the `.env` file:

```bash
// .env
CHAINLIT_AUTH_SECRET=...
CHAINLIT_USERNAME=OpenRAG
CHAINLIT_PASSWORD=OpenRAG2025
```

This configuration will enable secure access to your Chainlit application using the specified credentials.